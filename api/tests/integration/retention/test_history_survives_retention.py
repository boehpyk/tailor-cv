"""T18 — a registered user's tailoring history survives the guest purge and the orphan sweep, and the
exactly-one-owner CHECK is what keeps the cascade away from it (slice 2.3, AC-19, AC-20, AC-21).

**Tier: [proof].** The purge (`PurgeExpiredGuestSessions`, `SqlAlchemyExpiredGuestData`) and the sweep
(`ReclaimOrphanedFiles`, `LocalOrphanFileScanner`) are **not edited** by this slice. 2.3 made
`guest_session_id` nullable on `posting_job_posting`, `tailoring_run` and `export_job` (migration
`03494836ce30`), which fired 2.2's tripwire by design; these are the proofs that tripwire asked for,
the 2.3 siblings of 2.2's `test_registered_data_survives_purge_and_sweep.py` (whose conventions this
file follows: the rolled-back `session` fixture, a per-test `tmp_path` volume, `os.utime` ages,
every assertion read from the tables and the filesystem, never from a report). Each test was observed
red under its named mutation and the source restored byte-exact (`git diff --stat api/src` empty);
both outcomes are in each docblock.
"""

from __future__ import annotations

import os
import secrets
import stat
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Table, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.retention.purge_expired_guest_sessions import (
    PurgeExpiredGuestSessions,
)
from tailorcraft.application.retention.reclaim_orphaned_files import ReclaimOrphanedFiles
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFailureReason, ExportFormat
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    GuestSessionId,
    PasswordHash,
    UserId,
)
from tailorcraft.domain.retention.value_objects import RetentionWindow
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.files.orphan_scanner import LocalOrphanFileScanner
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
    SqlAlchemyExpiredGuestData,
)
from tailorcraft.infrastructure.retention.data_access import CommittingExpiredGuestDataAdapter
from tailorcraft.infrastructure.settings import Settings
from tests.integration.owners import pasted_posting, queued_export, ready_export, succeeded_run

_PASSWORD_HASH = PasswordHash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA")
_WINDOW_HOURS = 24
_GRACE = timedelta(hours=24)
_OLD_AGE = timedelta(hours=60)  # past the 24 h window + 24 h grace floor


def _assert_test_database(settings: Settings) -> None:
    assert "_test" in settings.database_url, (
        f"refusing to run a deleting retention-proof test against {settings.database_url!r}"
    )


async def _insert_guest_session(session: AsyncSession, *, expires_at: datetime) -> GuestSessionId:
    """A raw `INSERT`: `GuestSession.start` cannot place `expires_at` in the past."""
    session_id = GuestSessionId(uuid4())
    await session.execute(
        guest_session_table.insert().values(
            id=session_id,
            token_hash=secrets.token_hex(32),
            created_at=expires_at - timedelta(hours=24),
            expires_at=expires_at,
        )
    )
    return session_id


async def _new_user(session: AsyncSession, clock: FixedClock, email: str) -> UserId:
    users = SqlAlchemyUserRepository(session)
    user_id = users.next_identity()
    await users.add(
        User.register_with_password(
            id=user_id,
            email=EmailAddress.parse(email),
            password_hash=_PASSWORD_HASH,
            at=clock.now(),
        )
    )
    return user_id


class _Entry:
    """One history entry's rows: a posting, a run over it, and its export jobs."""

    def __init__(self, posting_id: object, run: TailoringRun, jobs: list[ExportJob]) -> None:
        self.posting_id = posting_id
        self.run = run
        self.jobs = jobs


async def _entry(
    session: AsyncSession,
    files: LocalFileStore,
    owner: Owner,
    at: datetime,
    formats: tuple[ExportFormat, ...],
) -> _Entry:
    """A posting, a succeeded run over it and one `ready` export per format, each with its bytes on
    the real volume at the job's derived key — the file every one of these rows names."""
    posting = pasted_posting(owner, at)
    await SqlAlchemyJobPostingRepository(session).add(posting)
    run = succeeded_run(owner, at, job_posting_id=posting.id)
    await SqlAlchemyTailoringRunRepository(session).add(run)
    jobs: list[ExportJob] = []
    for export_format in formats:
        job = ready_export(owner, run, at, format=export_format)
        await SqlAlchemyExportJobRepository(session).add(job)
        await files.put(job.storage_ref, f"bytes of {job.id.value}".encode())
        jobs.append(job)
    await session.flush()
    return _Entry(posting.id, run, jobs)


async def _row(session: AsyncSession, table: Table, row_id: object) -> dict[str, object] | None:
    """Every column of one row, read directly — never through the ORM's identity map."""
    row = (
        (await session.execute(select(table).where(table.c.id == row_id))).mappings().one_or_none()
    )
    return dict(row) if row is not None else None


async def _rows_of(session: AsyncSession, entries: list[_Entry]) -> dict[object, object]:
    snapshot: dict[object, object] = {}
    for entry in entries:
        snapshot[entry.posting_id] = await _row(session, job_posting_table, entry.posting_id)
        snapshot[entry.run.id] = await _row(session, tailoring_run_table, entry.run.id)
        for job in entry.jobs:
            snapshot[job.id] = await _row(session, export_job_table, job.id)
    return snapshot


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _age(path: Path, at: datetime) -> None:
    """The scanner reads `st_mtime` (2.2's T16 docblock), so age is set with `os.utime`."""
    timestamp = at.timestamp()
    os.utime(path, (timestamp, timestamp))


def _purge(
    session: AsyncSession, files: LocalFileStore, clock: FixedClock
) -> PurgeExpiredGuestSessions:
    return PurgeExpiredGuestSessions(
        data=CommittingExpiredGuestDataAdapter(SqlAlchemyExpiredGuestData(session), session),
        files=files,
        clock=clock,
        window=RetentionWindow(hours=_WINDOW_HOURS),
        batch_limit=10,
        dry_run=False,
    )


# --- AC-19: a user's history survives a full purge run -------------------------------------------


async def test_ac19_a_users_history_survives_a_full_purge_run(
    settings: Settings, session: AsyncSession, clock: FixedClock, tmp_path: Path
) -> None:
    """AC-19. One expired guest session with a posting, a run and a PDF export (file on a real
    volume); one user with 3 postings, 3 runs and 4 ready exports on the same volume, all 30 days
    old. A full purge deletes exactly the guest's three rows and one file; the user's 10 rows are
    **byte-identical** (every column, read directly) and the 4 files exist with identical bytes and
    mode `0600`.

    **Mutation, observed red on 2026-09-28 and reverted byte-exact.** Widened
    `SqlAlchemyExpiredGuestData.list_expired`'s `export_half`
    (`infrastructure/persistence/retention/expired_guest_data.py`) from
    `export_job_table.c.guest_session_id.in_(session_ids)` to
    `(export_job_table.c.guest_session_id.in_(session_ids)) | (export_job_table.c.guest_session_id.is_(None))`
    — "also gather every user's export keys". The user's jobs come back labelled
    `guest_session_id = NULL`, and the assembly loop indexes `files_by_session[None]`: the purge
    aborts before any `DELETE` or unlink rather than quietly destroying a user's files — the same
    shape 2.2's AC-15 mutation met on the CV half. Recorded red:
    ```
    >           files_by_session[owner].append(FileRef.for_export(job_id, job_format))
    E           KeyError: None
    src/tailorcraft/infrastructure/persistence/retention/expired_guest_data.py:237: KeyError
    1 failed, 3 deselected, 1 warning in 1.15s
    ```
    Restored with `git checkout -- …/expired_guest_data.py`; `git diff --stat api/src` empty; green.
    """
    _assert_test_database(settings)
    files = LocalFileStore(tmp_path)
    thirty_days_ago = clock.now() - timedelta(days=30)

    expired = await _insert_guest_session(session, expires_at=clock.now() - timedelta(hours=1))
    guest = await _entry(
        session, files, GuestOwner(expired), clock.now() - timedelta(hours=2), (ExportFormat.PDF,)
    )

    user = UserOwner(await _new_user(session, clock, "ac19-survivor@example.com"))
    kept = [
        await _entry(session, files, user, thirty_days_ago, (ExportFormat.PDF, ExportFormat.DOCX)),
        await _entry(session, files, user, thirty_days_ago, (ExportFormat.PDF,)),
        await _entry(session, files, user, thirty_days_ago, (ExportFormat.DOCX,)),
    ]
    kept_jobs = [job for entry in kept for job in entry.jobs]
    assert len(kept_jobs) == 4
    before_rows = await _rows_of(session, kept)
    assert len(before_rows) == 10
    assert all(row is not None for row in before_rows.values())
    before_bytes = {job.id: (tmp_path / job.storage_ref.key).read_bytes() for job in kept_jobs}
    assert all(_mode(tmp_path / job.storage_ref.key) == 0o600 for job in kept_jobs)

    await _purge(session, files, clock)()

    # the guest's session, rows and file are gone
    assert await _row(session, guest_session_table, expired) is None
    assert await _row(session, job_posting_table, guest.posting_id) is None
    assert await _row(session, tailoring_run_table, guest.run.id) is None
    assert await _row(session, export_job_table, guest.jobs[0].id) is None
    assert not (tmp_path / guest.jobs[0].storage_ref.key).exists()

    # the user's ten rows are byte-identical, and its four files untouched
    assert await _rows_of(session, kept) == before_rows
    for job in kept_jobs:
        path = tmp_path / job.storage_ref.key
        assert path.exists(), f"user export {job.id!r}'s file was unlinked by the purge"
        assert path.read_bytes() == before_bytes[job.id]
        assert _mode(path) == 0o600


# --- AC-20: …and survives the orphan sweep --------------------------------------------------------


async def test_ac20_a_users_history_survives_the_orphan_sweep(
    settings: Settings, session: AsyncSession, clock: FixedClock, tmp_path: Path
) -> None:
    """AC-20. The same user shape, every file aged past window + grace by `os.utime`. A real sweep
    reclaims a planted true orphan **and** a planted `failed`-job orphan (a user-owned job whose
    bytes a render wrote before it failed: `file_key IS NULL`, so no row names the file) and
    **leaves the 4 ready files**.

    **Mutation, observed red on 2026-09-28 and reverted byte-exact.** Added
    `export_job_table.c.guest_session_id.is_not(None)` to `which_are_referenced`'s export half
    (`infrastructure/persistence/retention/expired_guest_data.py`, inside `union_all(...)`) — "only a
    guest's export counts as referenced". Recorded red (this test's direct cross-check, before the
    sweep runs):
    ```
    >   assert referenced == frozenset(job.storage_ref for job in kept_jobs)
    E   AssertionError: assert frozenset() == frozenset({Fi...c5d1e.docx')})
    E     Extra items in the right set:
    E     FileRef(key='00/79/0079f227-375c-4c6c-8da8-f347ba2b6924.pdf')
    E     FileRef(key='bd/39/bd398581-a1af-4ceb-8b26-974c4c06ca6c.docx')
    E     FileRef(key='ef/1c/ef1cbd66-10a4-46f0-ab08-73a25776f844.pdf')
    E     FileRef(key='ee/13/ee13ff2f-fc0a-4014-8888-b16df81c5d1e.docx')
    ```
    Every user export key read as unreferenced; the sweep would have unlinked all four. Restored with
    `git checkout -- …/expired_guest_data.py`; `git diff --stat api/src` empty; green.
    """
    _assert_test_database(settings)
    files = LocalFileStore(tmp_path)
    scanner = LocalOrphanFileScanner(tmp_path)
    user = UserOwner(await _new_user(session, clock, "ac20-survivor@example.com"))
    kept = [
        await _entry(session, files, user, clock.now(), (ExportFormat.PDF, ExportFormat.DOCX)),
        await _entry(session, files, user, clock.now(), (ExportFormat.PDF,)),
        await _entry(session, files, user, clock.now(), (ExportFormat.DOCX,)),
    ]
    kept_jobs = [job for entry in kept for job in entry.jobs]

    # a failed job's leftover bytes: the row exists, but names no file
    failed = queued_export(user, kept[0].run, clock.now(), format=ExportFormat.DOCX)
    failed.mark_started(clock.now())
    failed.mark_failed(ExportFailureReason.RENDER_FAILED, clock.now())
    await SqlAlchemyExportJobRepository(session).add(failed)
    await files.put(failed.storage_ref, b"half a rendered docx")
    await session.flush()
    assert (await _row(session, export_job_table, failed.id) or {})["file_key"] is None

    # a genuine orphan: bytes at an export key no row has ever named
    orphan = queued_export(user, kept[1].run, clock.now()).storage_ref
    await files.put(orphan, b"nobody points at this")

    old = clock.now() - _OLD_AGE
    for ref in [*(job.storage_ref for job in kept_jobs), failed.storage_ref, orphan]:
        _age(tmp_path / ref.key, old)

    data = SqlAlchemyExpiredGuestData(session)
    referenced = await data.which_are_referenced(
        [*(job.storage_ref for job in kept_jobs), failed.storage_ref, orphan]
    )
    assert referenced == frozenset(job.storage_ref for job in kept_jobs)

    report = await ReclaimOrphanedFiles(
        scanner, data, files, clock, RetentionWindow(hours=_WINDOW_HOURS), _GRACE
    )()

    assert report.reclaimed == 2, f"expected the two planted orphans reclaimed, got {report!r}"
    assert not (tmp_path / orphan.key).exists()
    assert not (tmp_path / failed.storage_ref.key).exists()
    for job in kept_jobs:
        path = tmp_path / job.storage_ref.key
        assert path.exists(), f"ready export {job.id!r}'s file was reclaimed by the sweep"
        assert path.read_bytes() == f"bytes of {job.id.value}".encode()


# --- AC-21: the CHECK is what keeps the cascade away ---------------------------------------------


async def _both_owners_run(
    session: AsyncSession, clock: FixedClock, session_id: GuestSessionId, user_id: UserId
) -> TailoringRun:
    """A user-owned run through the repository, then given a `guest_session_id` too by a raw
    `UPDATE` — the only way to reach the shape, since the aggregate's owner is a sum type."""
    run = succeeded_run(UserOwner(user_id), clock.now())
    await SqlAlchemyTailoringRunRepository(session).add(run)
    await session.flush()
    await session.execute(
        tailoring_run_table.update()
        .where(tailoring_run_table.c.id == run.id)
        .values(guest_session_id=session_id)
    )
    return run


async def test_ac21_a_run_with_both_owners_is_refused_by_the_check(
    settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    """AC-21, the restored half: with `ck_tailoring_run_exactly_one_owner` in place, the write that
    would put a user's run inside a guest session's cascade fails, **by the CHECK's name**. There is
    no row a purge of that session could take."""
    _assert_test_database(settings)
    session_id = await _insert_guest_session(session, expires_at=clock.now() - timedelta(hours=1))
    user_id = await _new_user(session, clock, "ac21-check@example.com")

    with pytest.raises(DBAPIError) as exc_info:
        async with session.begin_nested():
            await _both_owners_run(session, clock, session_id, user_id)

    assert violated_constraint(exc_info.value) == "ck_tailoring_run_exactly_one_owner"


async def test_ac21_without_the_check_the_purge_takes_a_users_run(
    settings: Settings, session: AsyncSession, clock: FixedClock, tmp_path: Path
) -> None:
    """AC-21, the counterfactual — **always run**, like 2.2's AC-17 counterfactual. Inside this
    test's own rolled-back transaction (the scratch schema: never committed, gone at teardown),
    `ck_tailoring_run_exactly_one_owner` is dropped, a user-owned run is given a `guest_session_id`
    too, and that session is purged: the `ON DELETE CASCADE` takes the user's run. The CHECK — not
    any `WHERE` in the purge — is the only thing standing between the two.

    **Observed red, 2026-09-28.** The same setup with the test's final assertion written as the
    "spared" claim (`assert after is not None`) failed:
    ```
    >   assert after is not None, "the user's run must survive the purge"
    E   AssertionError: the user's run must survive the purge
    E   assert None is not None
    1 failed, 3 deselected, 1 warning in 0.58s
    ```
    — the run was gone. Restoring the CHECK (the test above) turns the setup itself into a
    `ck_tailoring_run_exactly_one_owner` violation, so "spared" holds by construction.
    """
    _assert_test_database(settings)
    await session.execute(
        text("ALTER TABLE tailoring_run DROP CONSTRAINT ck_tailoring_run_exactly_one_owner")
    )
    session_id = await _insert_guest_session(session, expires_at=clock.now() - timedelta(hours=1))
    user_id = await _new_user(session, clock, "ac21-counterfactual@example.com")
    run = await _both_owners_run(session, clock, session_id, user_id)
    assert await _row(session, tailoring_run_table, run.id) is not None

    await _purge(session, LocalFileStore(tmp_path), clock)()

    after = await _row(session, tailoring_run_table, run.id)
    assert after is None, (
        "without the CHECK, the purge's cascade must take the doubly-owned run — if it survived, "
        "something other than the CHECK is protecting it and this docblock needs re-checking"
    )
