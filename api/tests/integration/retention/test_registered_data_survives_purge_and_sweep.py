"""T16 — the proof ADR-0006 §3 promised: a registered user's saved CVs and their files survive both
the guest purge and the orphan sweep, and a working copy dies with its workspace without taking its
saved source down with it (AC-15, AC-16, AC-17; S-21, S-52…S-54).

**Tier: [proof].** The purge (`PurgeExpiredGuestSessions`) and the orphan sweep
(`ReclaimOrphanedFiles`) are **not edited** by this slice — `SqlAlchemyExpiredGuestData`'s own module
docstring states the design ("the less it knows about owners, the safer it is") and names AC-15/AC-16
as the tests that prove it. Red-first would be ceremony against code that already exists and is
already correct; instead each test below is **observed red under its named mutation**, the mutated
source restored byte-exact (`git diff --exit-code api/src` after this file's own qa work, and before
this commit), and the run re-observed green — both outcomes recorded in the test's own docblock
(CLAUDE.md: *an assertion that has never been observed failing is a docblock*).

**Why these tests can use the ordinary rolled-back `session`/`connection` fixtures, unlike
`test_purge_database.py`'s AC-13.** That file needs *genuine* cross-connection durability and lock
contention — two independent physical connections, one blocking the other for real. Nothing here
needs that: every assertion below is "after this use case ran, what does the database/filesystem
say", answered entirely within one test's own transaction tree, and `CommittingExpiredGuestDataAdapter
.delete_session`'s `session.commit()` releasing a SAVEPOINT on the shared `session` fixture is exactly
as meaningful for that question as a real commit would be. **Files are real regardless, and each test
below gets its own `tmp_path`** — never the dev volume, and deliberately **not** the session-scoped
`settings.upload_dir` every other file in this package shares: `LocalOrphanFileScanner.scan_older_than`
walks its *whole* root, and the first version of this file shared that root across every test in it.
AC-16 aged three saved files and a planted orphan to 60h old; by the time `test_s21` ran later in the
same session, those files (real, on disk) were older than the floor **and their rows were gone** —
rolled back with their test's own transaction — so `test_s21`'s sweep, walking the *shared* directory,
reclaimed all of them (`OrphanScanReport(scanned=4, reclaimed=4)`, not the `1` the test asserted) and
failed for a reason that had nothing to do with S-21's own claim. Each test's own `tmp_path` is a
pytest-managed, per-test, disposable directory — never seen by any other test — which removes the
leakage at the root rather than papering over it with per-test cleanup.

**File ages.** AC-15 does not need any — the purge's predicate is `guest_session.expires_at`, a
domain timestamp set directly via a raw `INSERT` (a session that already exists in the past, exactly
`test_purge_database.py`'s `_Rig.new_guest_session` does, since `GuestSession.start` cannot construct
a past `expires_at`). AC-16 needs the *orphan sweep's* age, which — contra an earlier draft of this
slice's technical plan — is **not** read from the UUIDv7 embedded timestamp: `LocalOrphanFileScanner
.scan_older_than` reads `st_mtime`, and its sibling test module
(`test_reclaim_orphaned_files_on_disk.py`) documents that this is deliberate ("the only clock this
sweep judges age by ... is deliberately not the UUID's own embedded timestamp"). This file follows
that same, real mechanism (`os.utime`) rather than a UUID timestamp that the shipped scanner does not
read — the technical plan's "the sweep reads age from the key" sentence describes a design the
implementation does not carry, and is worth flagging back to the plan's owner rather than silently
tested around.
"""

from __future__ import annotations

import os
import secrets
import stat
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.retention.purge_expired_guest_sessions import (
    PurgeExpiredGuestSessions,
)
from tailorcraft.application.retention.reclaim_orphaned_files import ReclaimOrphanedFiles
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    GuestSessionId,
    PasswordHash,
    UserId,
)
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    CvContentType,
    ExtractedText,
    OriginalFilename,
)
from tailorcraft.domain.retention.value_objects import RetentionWindow
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.files.orphan_scanner import LocalOrphanFileScanner
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
    SqlAlchemyExpiredGuestData,
)
from tailorcraft.infrastructure.retention.data_access import CommittingExpiredGuestDataAdapter
from tailorcraft.infrastructure.settings import Settings

_PASSWORD_HASH = PasswordHash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA")
_WINDOW_HOURS = 24
_GRACE = timedelta(hours=24)
_OLD_AGE = timedelta(hours=60)  # comfortably past a 24h window + 24h grace (48h floor)


def _assert_test_database(settings: Settings) -> None:
    assert "_test" in settings.database_url, (
        f"refusing to run a deleting retention-proof test against {settings.database_url!r}"
    )


async def _insert_guest_session(
    session: AsyncSession, *, expires_at: datetime, created_at: datetime | None = None
) -> GuestSessionId:
    """A raw `INSERT`, exactly `test_purge_database.py`'s `_Rig.new_guest_session`: `GuestSession
    .start` can only place `expires_at` in the future relative to `at`, and this file needs sessions
    already expired."""
    session_id = GuestSessionId(uuid4())
    await session.execute(
        guest_session_table.insert().values(
            id=session_id,
            token_hash=secrets.token_hex(32),
            created_at=created_at or (expires_at - timedelta(hours=24)),
            expires_at=expires_at,
        )
    )
    return session_id


async def _new_user(session: AsyncSession, clock: FixedClock, *, email: str) -> UserId:
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


async def _saved_cv_with_file(
    session: AsyncSession,
    files: LocalFileStore,
    owner: UserId,
    *,
    uploaded_at: datetime,
    content: bytes = b"%PDF-1.4 a saved cv",
) -> BaseCv:
    cvs = SqlAlchemyBaseCvRepository(session)
    cv_id = cvs.next_identity()
    cv = BaseCv.upload(
        id=cv_id,
        owner=UserOwner(owner),
        original_filename=OriginalFilename("saved.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=len(content),
        file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
        uploaded_at=uploaded_at,
    )
    cv.mark_extracted(ExtractedText("word " * 200), uploaded_at)
    await cvs.add(cv)
    await files.put(cv.file, content)
    return cv


async def _guest_cv_with_file(
    session: AsyncSession,
    files: LocalFileStore,
    owner: GuestSessionId,
    *,
    uploaded_at: datetime,
    content: bytes = b"%PDF-1.4 a guest cv",
) -> BaseCv:
    cvs = SqlAlchemyBaseCvRepository(session)
    cv_id = cvs.next_identity()
    cv = BaseCv.upload(
        id=cv_id,
        owner=GuestOwner(owner),
        original_filename=OriginalFilename("guest.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=len(content),
        file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
        uploaded_at=uploaded_at,
    )
    await cvs.add(cv)
    await files.put(cv.file, content)
    return cv


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _age(path: Path, at: datetime) -> None:
    """`os.utime`, matching `test_reclaim_orphaned_files_on_disk.py`'s helper exactly: the scanner
    reads `st_mtime`, never a UUID's embedded timestamp (this module's docstring explains why)."""
    timestamp = at.timestamp()
    os.utime(path, (timestamp, timestamp))


async def _full_row(session: AsyncSession, cv_id: object) -> dict[str, object]:
    """Every column of one `intake_base_cv` row, as a plain dict — the byte-identical comparison
    AC-15 asks for, read directly rather than through the ORM (which would hand back the *same*
    Python object from the identity map and prove nothing about what is actually stored)."""
    row = (
        (await session.execute(select(base_cv_table).where(base_cv_table.c.id == cv_id)))
        .mappings()
        .one()
    )
    return dict(row)


# --- AC-15 / S-52: a registered user's saved CVs survive a full purge run ------------------------


async def test_ac15_a_registered_users_saved_cvs_survive_a_full_purge_run(
    settings: Settings, session: AsyncSession, clock: FixedClock, tmp_path: Path
) -> None:
    """AC-15, S-52. Setup: one expired guest session owning two base CVs (real files), one live
    guest session, and one user owning three saved CVs whose files are on the same volume and whose
    `uploaded_at` is 30 days old. A full purge run deletes exactly the expired session's two rows and
    two files; the user's three rows are **byte-identical** (every column, read directly — never
    through `PurgeReport`, R-42) and their three files exist with identical bytes and mode `0600`.

    **Mutation, observed red on 2026-09-25 and reverted byte-exact.** Widened
    `SqlAlchemyExpiredGuestData.list_expired`'s `base_cv_half` predicate
    (`infrastructure/persistence/retention/expired_guest_data.py`, the `.where(...)` at the end of
    `base_cv_half`) from `base_cv_table.c.guest_session_id.in_(session_ids)` to
    `(base_cv_table.c.guest_session_id.in_(session_ids)) | (base_cv_table.c.guest_session_id.is_(None))`
    — i.e. "also gather every saved CV's key, regardless of which session is in this batch". Every
    saved CV's `guest_session_id` is `NULL`, so the widened half returns those rows too, each labelled
    with `guest_session_id = NULL`; `list_expired`'s assembly loop then does
    `files_by_session[owner].append(stored)` with `owner = None`, and `files_by_session` only has keys
    for the batch's real session ids — this is a `KeyError: None`, not a silent bad delete, because
    the *first* saved CV encountered aborts `list_expired` before any `DELETE` is issued. Recorded red
    verbatim (`pytest -k test_ac15 tests/integration/retention/test_registered_data_survives_purge_and
    _sweep.py`):
    ```
    for row in (await self._session.execute(union_all(base_cv_half, export_half))).all():
        owner: GuestSessionId = row.guest_session_id
        stored: FileRef | None = row.file_key
        if stored is not None:
    >       files_by_session[owner].append(stored)
            ^^^^^^^^^^^^^^^^^^^^^^^
    E       KeyError: None

    src/tailorcraft/infrastructure/persistence/retention/expired_guest_data.py:223: KeyError
    ...
    FAILED .../test_ac15_a_registered_users_saved_cvs_survive_a_full_purge_run
    1 failed, 4 deselected in 0.57s
    ```
    Source restored byte-exact (`git checkout -- api/src/tailorcraft/infrastructure/persistence/
    retention/expired_guest_data.py`, confirmed with `git diff --stat api/src` showing nothing); this
    test re-run green alone (`pytest -k test_ac15`) and the full 5-test module re-run green twice in a
    row afterward. **The mutation does not even need to reach a delete to prove the point**: a
    predicate that stops identifying a saved CV's guest as "not a guest" breaks the purge before it
    can do anything, which is the schema-level guarantee (S-52's "spared by schema, not by a WHERE")
    holding from the other side — there is no `WHERE` this adapter could write, wrong, that would
    quietly delete a saved CV;
    the only way to reach one at all is to make it *look* like a guest row, which crashes instead.
    """
    _assert_test_database(settings)
    files = LocalFileStore(tmp_path)

    expired_session = await _insert_guest_session(
        session, expires_at=clock.now() - timedelta(hours=1)
    )
    expired_cv_1 = await _guest_cv_with_file(
        session, files, expired_session, uploaded_at=clock.now(), content=b"expired one"
    )
    expired_cv_2 = await _guest_cv_with_file(
        session, files, expired_session, uploaded_at=clock.now(), content=b"expired two"
    )

    live_session = await _insert_guest_session(session, expires_at=clock.now() + timedelta(hours=1))

    user_id = await _new_user(session, clock, email="ac15-survivor@example.com")
    thirty_days_ago = clock.now() - timedelta(days=30)
    saved = [
        await _saved_cv_with_file(
            session, files, user_id, uploaded_at=thirty_days_ago, content=f"saved {i}".encode()
        )
        for i in range(3)
    ]
    await session.flush()

    before = {cv.id: await _full_row(session, cv.id) for cv in saved}
    before_bytes = {cv.id: (tmp_path / cv.file.key).read_bytes() for cv in saved}
    for cv in saved:
        assert _mode(tmp_path / cv.file.key) == 0o600, (
            "test setup: a saved CV's file must be 0600 before the purge for the 'mode preserved' "
            "half of this assertion to mean anything"
        )

    data = CommittingExpiredGuestDataAdapter(SqlAlchemyExpiredGuestData(session), session)
    purge = PurgeExpiredGuestSessions(
        data=data,
        files=files,
        clock=clock,
        window=RetentionWindow(hours=_WINDOW_HOURS),
        batch_limit=10,
        dry_run=False,
    )
    await purge()

    # the expired session and its two CVs/files are gone
    remaining_session = await session.execute(
        select(guest_session_table.c.id).where(guest_session_table.c.id == expired_session)
    )
    assert remaining_session.scalar_one_or_none() is None
    for cv in (expired_cv_1, expired_cv_2):
        remaining_cv = await session.execute(
            select(base_cv_table.c.id).where(base_cv_table.c.id == cv.id)
        )
        assert remaining_cv.scalar_one_or_none() is None
        assert not (tmp_path / cv.file.key).exists()

    # the live session is untouched
    remaining_live = await session.execute(
        select(guest_session_table.c.id).where(guest_session_table.c.id == live_session)
    )
    assert remaining_live.scalar_one_or_none() == live_session

    # the user's three saved CVs are byte-identical, rows and files, mode included
    for cv in saved:
        after = await _full_row(session, cv.id)
        assert after == before[cv.id], f"saved CV {cv.id!r}'s row changed across the purge"
        path = tmp_path / cv.file.key
        assert path.exists(), f"saved CV {cv.id!r}'s file was unlinked by the purge"
        assert path.read_bytes() == before_bytes[cv.id]
        assert _mode(path) == 0o600


# --- AC-16 / S-53: a registered user's saved CVs survive the orphan sweep ------------------------


async def test_ac16_a_registered_users_saved_cvs_survive_the_orphan_sweep(
    settings: Settings, session: AsyncSession, clock: FixedClock, tmp_path: Path
) -> None:
    """AC-16, S-53. Same user, three saved CVs whose files are aged well past window + grace (60h,
    against a 24h + 24h floor). A real, non-dry-run orphan sweep reclaims a planted true orphan and
    **leaves all three saved files**; `which_are_referenced` returns all three saved keys.

    **Mutation, observed red on 2026-09-25 and reverted byte-exact.** Added
    `base_cv_table.c.guest_session_id.is_not(None)` to `which_are_referenced`'s base-CV half
    (`infrastructure/persistence/retention/expired_guest_data.py`, inside `union_all(...)`) — "a saved
    CV's key does not count as referenced". This test's own `which_are_referenced` assertion (proved
    directly, before the sweep even runs — the cross-check is the mechanism AC-16 is about) is where
    the mutation shows up, verbatim:
    ```
    referenced = await data.which_are_referenced(keys)
    >   assert referenced == frozenset(cv.file for cv in saved), (
            "which_are_referenced must name every saved CV's key as referenced, and the orphan as not"
        )
    E   AssertionError: which_are_referenced must name every saved CV's key as referenced, and the
        orphan as not
    E   assert frozenset() == frozenset({FileRef(key='01/a0/...9a3.pdf'),
        FileRef(key='01/a0/...a08.pdf'), FileRef(key='01/a0/...13.pdf')})
    E     Extra items in the right set:
    E     FileRef(key='01/a0/01a0d98f-9d7c-71cb-b156-fce74c1fa9a3.pdf')
    E     FileRef(key='01/a0/01a0d98f-9d79-72ea-913c-d973e1046a08.pdf')
    E     FileRef(key='01/a0/01a0d98f-9d2c-7b35-9a6f-9cdff5a25f13.pdf')

    tests/integration/retention/test_registered_data_survives_purge_and_sweep.py:375: AssertionError
    1 failed, 4 deselected in 0.56s
    ```
    All three saved keys came back **unreferenced** — exactly the adapter's own docstring's warning
    ("a cross-check that learned to look only at guest rows ... would declare every saved file an
    orphan once it aged past the window — and saved files do not expire"); had the test instead run
    the sweep to completion under this mutation, those three files would have been unlinked along with
    the planted orphan. Source restored byte-exact (`git checkout --
    api/src/tailorcraft/infrastructure/persistence/retention/expired_guest_data.py`, confirmed clean
    with `git diff --stat api/src`); re-run green alone and the full 5-test module green twice in a
    row afterward.
    """
    _assert_test_database(settings)
    files = LocalFileStore(tmp_path)
    scanner = LocalOrphanFileScanner(tmp_path)

    user_id = await _new_user(session, clock, email="ac16-survivor@example.com")
    saved = [
        await _saved_cv_with_file(
            session, files, user_id, uploaded_at=clock.now(), content=f"ac16 saved {i}".encode()
        )
        for i in range(3)
    ]
    await session.flush()
    old = clock.now() - _OLD_AGE
    for cv in saved:
        _age(tmp_path / cv.file.key, old)

    # a genuine orphan: a file on the volume with no row naming it, also aged
    orphan_ref = FileRef.for_base_cv(BaseCvId(uuid4()), CvContentType.PDF)
    await files.put(orphan_ref, b"nobody points at this")
    _age(tmp_path / orphan_ref.key, old)

    data = SqlAlchemyExpiredGuestData(session)
    keys = [cv.file for cv in saved] + [orphan_ref]
    referenced = await data.which_are_referenced(keys)
    assert referenced == frozenset(cv.file for cv in saved), (
        "which_are_referenced must name every saved CV's key as referenced, and the orphan as not"
    )

    sweep = ReclaimOrphanedFiles(
        scanner,
        data,
        files,
        clock,
        RetentionWindow(hours=_WINDOW_HOURS),
        _GRACE,
    )
    report = await sweep()

    assert report.reclaimed == 1, f"expected exactly the planted orphan reclaimed, got {report!r}"
    assert not (tmp_path / orphan_ref.key).exists()
    for cv in saved:
        path = tmp_path / cv.file.key
        assert path.exists(), f"saved CV {cv.id!r}'s file was reclaimed by the orphan sweep"
        assert path.read_bytes().startswith(b"ac16 saved")


# --- AC-17 / S-54: a working copy dies with its workspace; its saved source does not --------------


async def test_ac17_a_working_copys_expiry_never_touches_its_saved_sources_row_or_file(
    settings: Settings, session: AsyncSession, clock: FixedClock, tmp_path: Path
) -> None:
    """AC-17, S-54. Copy a saved CV into a guest session via the real `BaseCv.copy_from` (I-8), then
    expire that session and purge: the copy's row and file are gone, the saved CV's row and file are
    untouched, and the two `file_key`s were never equal.

    This is the **green, unmutated** proof — the primary claim AC-17 makes about the shipped code.
    The counterfactual immediately below (`test_ac17_mutation_...`) is what shows *why* the two keys
    being distinct is load-bearing rather than incidental: it constructs the one scenario `BaseCv
    .copy_from`'s own guard (`file == source.file -> InvariantViolated`) and `uq_intake_base_cv_
    file_key` jointly make unreachable through ordinary code, and shows the purge would destroy the
    saved CV's bytes if it were ever reached.
    """
    _assert_test_database(settings)
    files = LocalFileStore(tmp_path)
    cvs = SqlAlchemyBaseCvRepository(session)

    user_id = await _new_user(session, clock, email="ac17-source@example.com")
    source = await _saved_cv_with_file(
        session, files, user_id, uploaded_at=clock.now(), content=b"the saved original"
    )
    await session.flush()

    copy_session = await _insert_guest_session(session, expires_at=clock.now() + timedelta(hours=1))
    copy_id = cvs.next_identity()
    copy_ref = FileRef.for_base_cv(copy_id, source.content_type)
    assert copy_ref != source.file, "test setup: FileRef.for_base_cv must derive distinct keys"
    copy = BaseCv.copy_from(
        source=source, id=copy_id, into=GuestOwner(copy_session), file=copy_ref, at=clock.now()
    )
    await cvs.add(copy)
    await files.put(copy.file, b"a working copy, its own bytes")
    await session.flush()

    # expire the copy's session
    await session.execute(
        guest_session_table.update()
        .where(guest_session_table.c.id == copy_session)
        .values(expires_at=clock.now() - timedelta(hours=1))
    )
    await session.flush()

    before_source_row = await _full_row(session, source.id)
    before_source_bytes = (tmp_path / source.file.key).read_bytes()

    data = CommittingExpiredGuestDataAdapter(SqlAlchemyExpiredGuestData(session), session)
    purge = PurgeExpiredGuestSessions(
        data=data,
        files=files,
        clock=clock,
        window=RetentionWindow(hours=_WINDOW_HOURS),
        batch_limit=10,
        dry_run=False,
    )
    await purge()

    remaining_copy = await session.execute(
        select(base_cv_table.c.id).where(base_cv_table.c.id == copy.id)
    )
    assert remaining_copy.scalar_one_or_none() is None, "the working copy's row must be gone"
    assert not (tmp_path / copy.file.key).exists(), "the working copy's file must be gone"

    after_source_row = await _full_row(session, source.id)
    assert after_source_row == before_source_row, "the saved source's row changed across the purge"
    source_path = tmp_path / source.file.key
    assert source_path.exists(), "the saved source's file was unlinked by the purge"
    assert source_path.read_bytes() == before_source_bytes


async def test_ac17_mutation_two_base_cvs_sharing_a_file_key_would_let_the_purge_unlink_the_saved_cvs_bytes(
    settings: Settings, session: AsyncSession, clock: FixedClock, tmp_path: Path
) -> None:
    """AC-17's named mutation — "make the copy use `FileRef.for_base_cv(source.id, ...)`" — cannot be
    reached through `BaseCv.copy_from` at all: that method's own guard refuses `file == source.file`
    (I-8) before any row is built, so mutating the *use case* to hand it the source's `FileRef` would
    just turn `CopySavedBaseCvToWorkspace` into a use case that always raises `InvariantViolated`, not
    into one that creates the dangerous row — there is no version of "make the copy reuse the
    source's key" reachable by editing application code alone. Reaching the scenario at all needs
    **both** locks removed: `copy_from`'s guard (bypassed here by building the "copy" through the
    plain `BaseCv.upload` constructor instead, which carries no such check) and
    `uq_intake_base_cv_file_key` (dropped for the width of this one test, inside its own rolled-back
    transaction — never committed, never touching `api/src`, and gone the instant this test's
    connection rolls back at teardown, same as everything else this suite does).

    This is therefore **not** the "edit `api/src`, observe red, revert" mutation the sibling AC-15/
    AC-16 tests carry — there is no line in `api/src` whose removal reproduces this bug, which is
    the point being proven: two independent locks, not one, stand between "a developer copy-pastes
    the wrong id" and a purge deleting a stranger's saved CV. What follows is a permanent,
    always-run counterfactual instead: with both locks removed, one confirms the purge's own
    behaviour (which is correct and unedited — only the *setup* is illegitimate) still does what its
    docstring says a guest-owned row's file gets: unlinked. The **saved** CV's bytes are the exact
    same bytes at the exact same key, so they go with it.
    """
    _assert_test_database(settings)
    files = LocalFileStore(tmp_path)
    cvs = SqlAlchemyBaseCvRepository(session)

    user_id = await _new_user(session, clock, email="ac17-mutation@example.com")
    source = await _saved_cv_with_file(
        session, files, user_id, uploaded_at=clock.now(), content=b"only copy of this saved cv"
    )
    await session.flush()

    # Drop the one lock a raw INSERT could still hit — scoped to this test's own transaction, and
    # gone at teardown along with everything else `session`/`connection` ever did.
    await session.execute(
        text("ALTER TABLE intake_base_cv DROP CONSTRAINT uq_intake_base_cv_file_key")
    )

    buggy_session = await _insert_guest_session(
        session, expires_at=clock.now() + timedelta(hours=1)
    )
    buggy_copy_id = cvs.next_identity()
    # Bypasses `copy_from`'s I-8 guard entirely by never calling it: `BaseCv.upload` has no rule
    # against reusing another row's file key, which is exactly why the guard has to live on
    # `copy_from` and not on `upload` — the aggregate that should never share a key is the one this
    # constructor is not aware of.
    buggy_copy = BaseCv.upload(
        id=buggy_copy_id,
        owner=GuestOwner(buggy_session),
        original_filename=OriginalFilename("buggy-copy.pdf"),
        content_type=source.content_type,
        size_bytes=source.size_bytes,
        file=source.file,  # the bug: the source's own key, not a fresh one
        uploaded_at=clock.now(),
    )
    await cvs.add(buggy_copy)
    await session.flush()

    await session.execute(
        guest_session_table.update()
        .where(guest_session_table.c.id == buggy_session)
        .values(expires_at=clock.now() - timedelta(hours=1))
    )
    await session.flush()

    data = CommittingExpiredGuestDataAdapter(SqlAlchemyExpiredGuestData(session), session)
    purge = PurgeExpiredGuestSessions(
        data=data,
        files=files,
        clock=clock,
        window=RetentionWindow(hours=_WINDOW_HOURS),
        batch_limit=10,
        dry_run=False,
    )
    await purge()

    # the danger, realised: the saved CV's row survives (a different id, a different table row) —
    # but its bytes do not, because the "copy" pointed at them.
    after_source_row = await _full_row(session, source.id)
    assert after_source_row["id"] == source.id
    assert not (tmp_path / source.file.key).exists(), (
        "the saved CV's bytes must have been destroyed by this construction for the counterfactual "
        "to prove anything — if this assertion fails, the two independent locks are doing more work "
        "than this test assumed and the docstring above needs re-checking, not this assertion"
    )


# --- S-21: the crash-window survivor (row deleted, file left behind) is reclaimed -----------------


async def test_s21_a_saved_cvs_row_deleted_without_its_file_is_reclaimed_by_the_orphan_sweep(
    settings: Settings, session: AsyncSession, clock: FixedClock, tmp_path: Path
) -> None:
    """S-21: "Delete: the process dies after the commit, before the unlink" — constructed rather than
    simulated, exactly the failure contract's own words: the row is removed (mirroring
    `DeleteSavedBaseCv`'s Core `DELETE … WHERE id AND user_id`) but the file is deliberately never
    unlinked, standing in for a crash between the two. Aged past window + grace and run through
    AC-16's real harness: with no row naming it any more, `which_are_referenced` does not return it,
    and the sweep reclaims it as a genuine orphan — the file the operator's `purge-guests --orphans`
    is there to find.
    """
    _assert_test_database(settings)
    files = LocalFileStore(tmp_path)
    scanner = LocalOrphanFileScanner(tmp_path)
    cvs = SqlAlchemyBaseCvRepository(session)

    user_id = await _new_user(session, clock, email="s21-crash-window@example.com")
    cv = await _saved_cv_with_file(
        session, files, user_id, uploaded_at=clock.now(), content=b"orphaned by a crash"
    )
    await session.flush()

    # The row half of "rows first, committed, then files": remove the row, never unlink the file —
    # the exact survivor a crash between the two would leave.
    await cvs.remove(cv.id, UserOwner(user_id))
    await session.flush()

    remaining = await session.execute(select(base_cv_table.c.id).where(base_cv_table.c.id == cv.id))
    assert remaining.scalar_one_or_none() is None, "test setup: the row must actually be gone"
    path = tmp_path / cv.file.key
    assert path.exists(), "test setup: the file must still be on disk (the crash's survivor)"
    _age(path, clock.now() - _OLD_AGE)

    data = SqlAlchemyExpiredGuestData(session)
    referenced = await data.which_are_referenced([cv.file])
    assert cv.file not in referenced, "a deleted row's key must not read as referenced"

    sweep = ReclaimOrphanedFiles(
        scanner, data, files, clock, RetentionWindow(hours=_WINDOW_HOURS), _GRACE
    )
    report = await sweep()

    assert report.reclaimed == 1
    assert not path.exists(), "the crash-window survivor must be reclaimed by the orphan sweep"
