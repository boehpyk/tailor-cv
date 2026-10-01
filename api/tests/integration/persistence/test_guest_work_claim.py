"""Persistence tests for `SqlAlchemyGuestWorkClaim` and `CommittingGuestWorkClaim` (slice 2.4, T18,
written after; AC-5 … AC-10, plus R-2's sibling). AC-12 (each guest `add` translating its guest-FK
refusal) is `test_owned_repositories.py`'s, commit 24c503b, and is not repeated here.

Every claim is made against rows written through the real repositories into a real Postgres. The
fixtures commit their seeds (a seed that only flushes dies with a refused request, CLAUDE.md); the
outer transaction still isolates the test, and every assertion is scoped to ids the test created.

**Mutation record** (T18 — a test-after test proves it can fail by being made to). Each mutation was
applied to `guest_work_claim.py` / `claim_access.py` by hand, the named test watched going red, and
the source restored byte-exact (`git diff` empty):

- Re-point the run UPDATE at `export_job` (the run UPDATE effectively dropped): 6 red, among them
  `test_every_table_with_a_guest_session_id_column_is_re_keyed_by_name` ("transfer does not re-key
  ['tailoring_run']"), `test_transfer_sends_exactly_six_statements_in_the_documented_order`,
  `test_nothing_but_the_owner_moves_...` and the `[tailoring_run]` AC-9 case.
- `.values(guest_session_id=None, user_id=user_id, version=tailoring_run_table.c.version + 1)` on the
  run UPDATE: `test_transfer_never_writes_a_column_but_the_two_owner_columns` red (the SET clause names
  `version`) and `test_nothing_but_the_owner_moves_...` red (`tailoring_run.version` changed); 2 red.
- Remove `await self._session.commit()` from `CommittingGuestWorkClaim.transfer`:
  `test_a_committed_claim_is_visible_to_another_connection_when_transfer_returns` and
  `test_a_commit_that_fails_...` red (no commit call, so the injected fault is never reached); 2 red.
- `lock_session` without `.with_for_update()`: `test_lock_session_takes_the_row_for_update` red.
- Drop the `copied_from_base_cv_id IS NULL` restriction: `test_a_working_copy_is_dropped_not_claimed`,
  the six-statement order test and the counts test red; 3 red.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
import pytest_asyncio
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Table, event, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.orm import Session

from tailorcraft.domain.export.value_objects import ExportJobId
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import BaseCvId, BaseCvLabel
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.identity.claim_access import CommittingGuestWorkClaim
from tailorcraft.infrastructure.persistence.database import create_engine
from tailorcraft.infrastructure.persistence.identity.guest_work_claim import (
    SqlAlchemyGuestWorkClaim,
)
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.registry import metadata
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.integration.owners import (
    extracted_cv,
    pasted_posting,
    ready_export,
    succeeded_run,
)
from tests.integration.persistence.owner_rows import persist_user

_OWNED_TABLES: Final[dict[str, Table]] = {
    "intake_base_cv": base_cv_table,
    "posting_job_posting": job_posting_table,
    "tailoring_run": tailoring_run_table,
    "export_job": export_job_table,
}
_OWNER_COLUMNS: Final = frozenset({"guest_session_id", "user_id"})
_TOKEN_A: Final = "ab" * 32
_TOKEN_B: Final = "cd" * 32


@dataclass(frozen=True, slots=True)
class _Seeded:
    """Ids of one fully populated guest session and what it owns."""

    guest: GuestSessionId
    token: str
    saved_cv_id: BaseCvId
    posting_id: JobPostingId
    run_id: TailoringRunId
    export_id: ExportJobId
    copy_id: BaseCvId
    copy_key: str
    claimed_cv_key: str
    other_user: UserId


async def _seed_session(
    session: AsyncSession, clock: FixedClock, *, token_hash: str | None = None
) -> _Seeded:
    """A guest session owning one of everything, plus one working copy of another user's saved CV.
    Committed (a seed that only flushes dies with a refused request)."""
    token = token_hash or secrets.token_hex(32)
    guests = SqlAlchemyGuestSessionRepository(session)
    guest = GuestSession.start(
        id=guests.next_identity(),
        token_hash=token,
        at=clock.now(),
        ttl_hours=24,
    )
    await guests.add(guest)
    owner = GuestOwner(guest.id)
    at = clock.now()

    cvs = SqlAlchemyBaseCvRepository(session)
    cv = extracted_cv(owner, at)
    await cvs.add(cv)

    postings = SqlAlchemyJobPostingRepository(session)
    posting = pasted_posting(owner, at)
    await postings.add(posting)

    runs = SqlAlchemyTailoringRunRepository(session)
    run = succeeded_run(owner, at, base_cv_id=cv.id, job_posting_id=posting.id)
    await runs.add(run)

    exports = SqlAlchemyExportJobRepository(session)
    export = ready_export(owner, run, at)
    await exports.add(export)

    # A working copy: a copy of a CV another account keeps (ADR-0022), never claimed.
    other = await persist_user(session, clock)
    source = extracted_cv(other, at)
    source.rename(BaseCvLabel("Saved original"), at)
    source.release_events()
    await cvs.add(source)
    copy_id = cvs.next_identity()
    copy = BaseCv.copy_from(
        source=source,
        id=copy_id,
        into=owner,
        file=FileRef.for_base_cv(copy_id, source.content_type),
        at=at,
    )
    copy.release_events()
    await cvs.add(copy)
    await session.commit()
    return _Seeded(
        guest=guest.id,
        token=token,
        saved_cv_id=cv.id,
        posting_id=posting.id,
        run_id=run.id,
        export_id=export.id,
        copy_id=copy.id,
        copy_key=copy.file.key,
        claimed_cv_key=cv.file.key,
        other_user=other.user_id,
    )


async def _row(session: AsyncSession, table: Table, row_id: Any) -> dict[str, Any]:
    result = await session.execute(select(table).where(table.c.id == row_id))
    return dict(result.mappings().one())


async def _snapshot(session: AsyncSession, seeded: _Seeded) -> dict[str, dict[str, Any]]:
    ids = {
        "intake_base_cv": seeded.saved_cv_id,
        "posting_job_posting": seeded.posting_id,
        "tailoring_run": seeded.run_id,
        "export_job": seeded.export_id,
    }
    return {name: await _row(session, _OWNED_TABLES[name], row_id) for name, row_id in ids.items()}


class _Capture:
    """Statements sent to Postgres through `engine`, between `__enter__` and `__exit__`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self.statements: list[str] = []

    def _on(self, conn: object, cursor: object, statement: str, *_args: object) -> None:
        self.statements.append(" ".join(statement.split()))

    def __enter__(self) -> _Capture:
        event.listen(self._engine.sync_engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(self._engine.sync_engine, "before_cursor_execute", self._on)


async def _user_for_claim(session: AsyncSession, clock: FixedClock) -> UserId:
    return (await persist_user(session, clock)).user_id


# --- AC-5: lock_session -----------------------------------------------------------------------


async def test_lock_session_returns_the_session_with_that_token_hash_and_no_other(
    session: AsyncSession, clock: FixedClock
) -> None:
    first = await _seed_session(session, clock, token_hash=_TOKEN_A)
    second = await _seed_session(session, clock, token_hash=_TOKEN_B)

    locked = await SqlAlchemyGuestWorkClaim(session).lock_session(_TOKEN_B)

    assert locked is not None
    assert locked.id == second.guest
    assert locked.id != first.guest


async def test_lock_session_returns_none_for_an_unknown_token_hash(
    session: AsyncSession, clock: FixedClock
) -> None:
    await _seed_session(session, clock)

    assert await SqlAlchemyGuestWorkClaim(session).lock_session("ef" * 32) is None


async def test_lock_session_takes_the_row_for_update(
    session: AsyncSession, engine: AsyncEngine, clock: FixedClock
) -> None:
    """AC-5. The statement carries `FOR UPDATE` and filters on `token_hash`. (The lock *behaviour*,
    a second claim or the purge waiting on it, is AC-14/AC-16's, with two real connections.)"""
    await _seed_session(session, clock)

    with _Capture(engine) as capture:
        await SqlAlchemyGuestWorkClaim(session).lock_session(_TOKEN_A)

    selects = [s for s in capture.statements if s.startswith("SELECT")]
    assert len(selects) == 1, capture.statements
    assert "FROM identity_guest_session" in selects[0]
    assert "token_hash = $1" in selects[0]
    assert selects[0].endswith("FOR UPDATE"), selects[0]


# --- AC-6: the six statements ---------------------------------------------------------------


def _verb_and_table(statement: str) -> tuple[str, str]:
    match = re.match(r"(UPDATE|DELETE FROM) (\w+)", statement)
    assert match is not None, statement
    return match.group(1), match.group(2)


async def _capture_transfer(
    session: AsyncSession, engine: AsyncEngine, clock: FixedClock
) -> tuple[_Seeded, UserId, list[str], Any]:
    seeded = await _seed_session(session, clock)
    user_id = await _user_for_claim(session, clock)
    adapter = SqlAlchemyGuestWorkClaim(session)
    assert (
        await adapter.lock_session(seeded.token) is not None
    )  # loaded, so the expunge is exercised
    with _Capture(engine) as capture:
        claimed = await adapter.transfer(seeded.guest, user_id)
    return seeded, user_id, capture.statements, claimed


async def test_transfer_sends_exactly_six_statements_in_the_documented_order(
    session: AsyncSession, engine: AsyncEngine, clock: FixedClock
) -> None:
    _, _, statements, _ = await _capture_transfer(session, engine, clock)

    assert [_verb_and_table(s) for s in statements] == [
        ("UPDATE", "intake_base_cv"),
        ("UPDATE", "posting_job_posting"),
        ("UPDATE", "tailoring_run"),
        ("UPDATE", "export_job"),
        ("DELETE FROM", "intake_base_cv"),
        ("DELETE FROM", "identity_guest_session"),
    ], statements
    assert "copied_from_base_cv_id IS NULL" in statements[0]
    assert statements[4].endswith("RETURNING intake_base_cv.file_key"), statements[4]
    for update in statements[:4]:
        assert "WHERE" in update, update
        assert "guest_session_id = $" in update.split("WHERE")[1], update


async def test_transfer_never_writes_a_column_but_the_two_owner_columns(
    session: AsyncSession, engine: AsyncEngine, clock: FixedClock
) -> None:
    """AC-7's statement half, and the load-bearing one for the worker (ADR-0025 fact 3): a `SET`
    that names `version` would turn a paid result into `SKIPPED`."""
    _, _, statements, _ = await _capture_transfer(session, engine, clock)

    for update in statements[:4]:
        set_clause = update.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
        written = {part.split("=")[0].strip() for part in set_clause.split(",")}
        assert written == _OWNER_COLUMNS, update


async def test_every_table_with_a_guest_session_id_column_is_re_keyed_by_name(
    session: AsyncSession, engine: AsyncEngine, clock: FixedClock
) -> None:
    """R-2's sibling. The tables are discovered from `information_schema`, not listed: a future
    guest-owned table the claim forgets would otherwise be cascade-deleted with its session, silently."""
    rows = await session.execute(
        text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'guest_session_id'"
        )
    )
    guest_owned = {r[0] for r in rows}
    assert guest_owned, "discovery found no guest-owned table; the query is wrong"

    _, _, statements, _ = await _capture_transfer(session, engine, clock)

    re_keyed = {t for verb, t in map(_verb_and_table, statements) if verb == "UPDATE"}
    missing = guest_owned - re_keyed
    assert not missing, (
        f"transfer does not re-key {sorted(missing)}; the session DELETE cascades them"
    )


async def test_transfer_counts_equal_the_rows_each_statement_touched(
    session: AsyncSession, engine: AsyncEngine, clock: FixedClock
) -> None:
    seeded, _, _, claimed = await _capture_transfer(session, engine, clock)

    assert (
        claimed.base_cvs,
        claimed.job_postings,
        claimed.tailoring_runs,
        claimed.export_jobs,
        claimed.working_copies_dropped,
    ) == (1, 1, 1, 1, 1)
    assert claimed.files_to_unlink == (FileRef(seeded.copy_key),)


async def test_a_working_copy_is_dropped_not_claimed(
    session: AsyncSession, engine: AsyncEngine, clock: FixedClock
) -> None:
    seeded, user_id, _, _ = await _capture_transfer(session, engine, clock)

    gone = await session.execute(
        select(base_cv_table.c.id).where(base_cv_table.c.id == seeded.copy_id)
    )
    assert gone.first() is None
    survivor = await _row(session, base_cv_table, seeded.saved_cv_id)
    assert survivor["user_id"] == user_id
    # The source the copy was made from, another account's, is untouched.
    sources = await session.execute(
        select(base_cv_table.c.user_id).where(base_cv_table.c.user_id == seeded.other_user)
    )
    assert len(sources.all()) == 1


async def test_transfer_deletes_the_session_row_and_expunges_the_loaded_aggregate(
    session: AsyncSession, engine: AsyncEngine, clock: FixedClock
) -> None:
    seeded, _, _, _ = await _capture_transfer(session, engine, clock)

    assert not any(isinstance(o, GuestSession) for o in session.identity_map.values())
    remaining = await session.execute(
        select(guest_session_table.c.id).where(guest_session_table.c.id == seeded.guest)
    )
    assert remaining.first() is None
    await session.flush()  # a stale instance in the map would flush against a deleted row


async def test_transfer_leaves_other_sessions_rows_alone(
    session: AsyncSession, clock: FixedClock
) -> None:
    mine = await _seed_session(session, clock, token_hash=_TOKEN_A)
    theirs = await _seed_session(session, clock, token_hash=_TOKEN_B)
    user_id = await _user_for_claim(session, clock)

    await SqlAlchemyGuestWorkClaim(session).transfer(mine.guest, user_id)

    row = await _row(session, tailoring_run_table, theirs.run_id)
    assert row["guest_session_id"] == theirs.guest
    assert row["user_id"] is None
    assert await _row(session, base_cv_table, theirs.copy_id)


# --- AC-7: nothing but the owner moves --------------------------------------------------------


async def test_nothing_but_the_owner_moves_column_by_column_and_no_file_changes(
    session: AsyncSession, clock: FixedClock, tmp_path: Path
) -> None:
    seeded = await _seed_session(session, clock)
    user_id = await _user_for_claim(session, clock)
    file = tmp_path / "claimed-cv.pdf"
    file.write_bytes(b"%PDF-1.4 the claimed CV's bytes")
    file.chmod(0o640)
    before_stat = file.stat()
    before = await _snapshot(session, seeded)
    assert before["tailoring_run"]["version"] >= 1
    for name, row in before.items():
        assert row["guest_session_id"] == seeded.guest, name
        assert row["user_id"] is None, name

    await SqlAlchemyGuestWorkClaim(session).transfer(seeded.guest, user_id)

    after = await _snapshot(session, seeded)
    for name in _OWNED_TABLES:
        assert after[name].keys() == before[name].keys()
        assert after[name]["guest_session_id"] is None, name
        assert after[name]["user_id"] == user_id, name
        for column, value in before[name].items():
            if column in _OWNER_COLUMNS:
                continue
            assert after[name][column] == value, f"{name}.{column} changed in a claim"
    after_stat = file.stat()
    assert (after_stat.st_ino, after_stat.st_mode, after_stat.st_size, after_stat.st_mtime_ns) == (
        before_stat.st_ino,
        before_stat.st_mode,
        before_stat.st_size,
        before_stat.st_mtime_ns,
    )
    assert file.read_bytes() == b"%PDF-1.4 the claimed CV's bytes"


# --- AC-9: a user-FK refusal is UserNotFound ----------------------------------------------------


@pytest.mark.parametrize("table_name", list(_OWNED_TABLES))
async def test_a_user_that_does_not_exist_is_user_not_found_for_each_owned_table(
    session: AsyncSession, clock: FixedClock, table_name: str
) -> None:
    """AC-9, once per FK: only that table's guest row is left, so the refusal comes from exactly
    its `fk_<table>_user_id_identity_user` (the earlier UPDATEs touch no row, so check no FK)."""
    seeded = await _seed_session(session, clock)
    keep = {
        "intake_base_cv": seeded.saved_cv_id,
        "posting_job_posting": seeded.posting_id,
        "tailoring_run": seeded.run_id,
        "export_job": seeded.export_id,
    }
    for name, table in _OWNED_TABLES.items():
        if name != table_name:
            await session.execute(table.delete().where(table.c.id == keep[name]))
    await session.commit()
    ghost = UserId(value=UUID("00000000-0000-7000-8000-00000000dead"))

    with pytest.raises(UserNotFound) as raised:
        await SqlAlchemyGuestWorkClaim(session).transfer(seeded.guest, ghost)

    assert raised.value.__cause__ is None  # `from None`: the driver's chain is unreachable
    await session.rollback()
    row = await _row(session, _OWNED_TABLES[table_name], keep[table_name])
    assert row["guest_session_id"] == seeded.guest


# --- AC-8: the commit ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def committed_engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    """An engine on the TEST database, built by the production `create_engine` (so its error-sanitising
    listener is the real one, which `violated_constraint` depends on), whose commits really commit,
    so a second connection can observe them. Refuses any URL not naming a `_test` database before a statement is sent."""
    assert "_test" in settings.test_database_url, settings.test_database_url
    engine = create_engine(settings.model_copy(update={"database_url": settings.test_database_url}))
    try:
        yield engine
    finally:
        await engine.dispose()


@dataclass
class _Committed:
    seeded: _Seeded
    user_id: UserId
    uploads: list[Path] = field(default_factory=list)


async def _erase(engine: AsyncEngine, seeded: _Seeded, user_id: UserId) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            guest_session_table.delete().where(guest_session_table.c.id == seeded.guest)
        )
        for uid in (user_id.value, seeded.other_user.value):
            await conn.execute(text("DELETE FROM identity_user WHERE id = :u"), {"u": uid})


async def _read_owner(engine: AsyncEngine, table: Table, row_id: Any) -> tuple[Any, Any]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(table.c.guest_session_id, table.c.user_id).where(table.c.id == row_id)
            )
        ).one()
        return row[0], row[1]


async def test_a_committed_claim_is_visible_to_another_connection_when_transfer_returns(
    committed_engine: AsyncEngine, clock: FixedClock
) -> None:
    """AC-8, first half: `CommittingGuestWorkClaim.transfer` commits before it returns."""
    factory = async_sessionmaker(committed_engine, expire_on_commit=False)
    async with factory() as seed_session:
        seeded = await _seed_session(seed_session, clock)
        user_id = await _user_for_claim(seed_session, clock)
        await seed_session.commit()
    try:
        async with factory() as claim_session:
            adapter = CommittingGuestWorkClaim(
                SqlAlchemyGuestWorkClaim(claim_session), claim_session
            )
            assert await adapter.lock_session(seeded.token) is not None
            await adapter.transfer(seeded.guest, user_id)
            # Read on a DIFFERENT connection, while the claiming session is still open.
            assert await _read_owner(committed_engine, tailoring_run_table, seeded.run_id) == (
                None,
                user_id,
            )
        async with committed_engine.connect() as conn:
            left = await conn.execute(
                select(guest_session_table.c.id).where(guest_session_table.c.id == seeded.guest)
            )
            assert left.first() is None
    finally:
        await _erase(committed_engine, seeded, user_id)


async def test_a_commit_that_fails_leaves_everything_guest_owned_and_touches_no_file(
    committed_engine: AsyncEngine,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC-8, second half. The fault is injected BELOW the adapter: `Session.commit`, the
    library call the wrapper's `AsyncSession.commit()` ends in, raises as a lost connection would. The
    wrapper and the inner adapter are the real ones, so nothing they do is bypassed. After the
    caller's rollback another connection finds every row guest-owned and the session present."""
    factory = async_sessionmaker(committed_engine, expire_on_commit=False)
    async with factory() as seed_session:
        seeded = await _seed_session(seed_session, clock)
        user_id = await _user_for_claim(seed_session, clock)
        await seed_session.commit()
    file = tmp_path / "claimed-cv.pdf"
    file.write_bytes(b"bytes")
    before_stat = file.stat()
    try:
        async with factory() as claim_session:
            adapter = CommittingGuestWorkClaim(
                SqlAlchemyGuestWorkClaim(claim_session), claim_session
            )
            assert await adapter.lock_session(seeded.token) is not None

            def _refuse(self: Session, *args: object, **kwargs: object) -> None:
                raise OperationalError("COMMIT", None, ConnectionError("connection lost"))

            monkeypatch.setattr(Session, "commit", _refuse)
            with pytest.raises(OperationalError):
                await adapter.transfer(seeded.guest, user_id)
            monkeypatch.undo()
            await claim_session.rollback()

        for name, table in _OWNED_TABLES.items():
            row_id = {
                "intake_base_cv": seeded.saved_cv_id,
                "posting_job_posting": seeded.posting_id,
                "tailoring_run": seeded.run_id,
                "export_job": seeded.export_id,
            }[name]
            assert await _read_owner(committed_engine, table, row_id) == (
                seeded.guest,
                None,
            ), name
        assert await _read_owner(committed_engine, base_cv_table, seeded.copy_id) == (
            seeded.guest,
            None,
        )
        async with committed_engine.connect() as conn:
            present = await conn.execute(
                select(guest_session_table.c.id).where(guest_session_table.c.id == seeded.guest)
            )
            assert present.first() is not None
        assert file.stat().st_ino == before_stat.st_ino
        assert file.read_bytes() == b"bytes"
    finally:
        monkeypatch.undo()
        await _erase(committed_engine, seeded, user_id)


async def test_a_transfer_that_raises_is_not_committed(
    committed_engine: AsyncEngine, clock: FixedClock
) -> None:
    """AC-8's sibling: the wrapper commits only a transfer that returned. A `UserNotFound` leaves
    the transaction for the caller's rollback; nothing durable moved."""
    factory = async_sessionmaker(committed_engine, expire_on_commit=False)
    async with factory() as seed_session:
        seeded = await _seed_session(seed_session, clock)
        user_id = await _user_for_claim(seed_session, clock)
        await seed_session.commit()
    ghost = UserId(value=UUID("00000000-0000-7000-8000-00000000beef"))
    try:
        async with factory() as claim_session:
            adapter = CommittingGuestWorkClaim(
                SqlAlchemyGuestWorkClaim(claim_session), claim_session
            )
            with pytest.raises(UserNotFound):
                await adapter.transfer(seeded.guest, ghost)
            await claim_session.rollback()
        assert await _read_owner(committed_engine, tailoring_run_table, seeded.run_id) == (
            seeded.guest,
            None,
        )
    finally:
        await _erase(committed_engine, seeded, user_id)


# --- AC-10: no migration -----------------------------------------------------------------------


async def test_autogenerate_against_head_produces_an_empty_diff(
    connection: AsyncConnection,
) -> None:
    """AC-10: the claim needs no schema change, so the mapped metadata and the migrated database
    agree exactly (the same options `alembic/env.py` runs autogenerate with)."""

    def _diff(sync_conn: Connection) -> Sequence[object]:
        context = MigrationContext.configure(
            sync_conn, opts={"compare_type": True, "compare_server_default": True}
        )
        return list(compare_metadata(context, metadata))

    assert await connection.run_sync(_diff) == []


@pytest.mark.parametrize("table_name", list(_OWNED_TABLES))
async def test_each_owned_table_has_its_exactly_one_owner_check_and_guest_session_index(
    session: AsyncSession, table_name: str
) -> None:
    """AC-10, read from the catalogs by name — never from the comments that promise them."""
    check = (
        await session.execute(
            text(
                "SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint "
                "WHERE conname = :n AND contype = 'c'"
            ),
            {"n": f"ck_{table_name}_exactly_one_owner"},
        )
    ).one()
    assert "num_nonnulls(guest_session_id, user_id) = 1" in check[0]
    assert check[1] is True

    index = (
        await session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :n"),
            {"n": f"ix_{table_name}_guest_session_id"},
        )
    ).scalar_one()
    assert f"ON public.{table_name} USING btree (guest_session_id)" in index
