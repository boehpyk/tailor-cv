"""`tailorcraft.cli purge-guests` — the CLI contract (T24, test-after and mutation-verified).

T23 shipped `run_from_cli` / `_purge_guests` / `_reclaim_orphans` (`infrastructure/retention/
purge_command.py`) before this file existed, so there is no red to record here: reverting a working
CLI to manufacture one would prove only that a file was absent, never that an assertion
discriminates. Every test below is instead proved to discriminate by **mutation** — see the PR/task
notes for the four recorded mutation failures, each produced by temporarily breaking the behaviour
a specific test guards, watching that test (and no unrelated one) fail, then restoring the file
exactly.

**The one rule that dominates this file, from CLAUDE.md's 1.4 incident.** `get_settings()` under
`APP_ENV=test` still returns the **dev** `database_url` — only `conftest.py`'s `settings` fixture
swaps in `test_database_url`. `run_from_cli` calls `get_settings()` itself, so no test here ever
calls `run_from_cli`: every deleting test calls `_purge_guests` / `_reclaim_orphans` directly,
injecting the already-swapped `settings` fixture through the seam `T23` built for exactly this
(`Settings` is a parameter, not a module-level read). `_assert_test_database` asserts `"_test"` is in
the URL about to be used, before the first statement, in every test that touches the database.

**Why setup here cannot use the ordinary rolled-back `session` fixture.** `_purge_guests` builds its
**own** engine and its own connection inside `_purge_use_case` — that is the same composition-root
shape production uses, and it is deliberately not shared with the test's own `connection`/`session`
fixture. A row inserted through that fixture lives in a SAVEPOINT on a *different* connection, and
under READ COMMITTED the CLI's own connection cannot see it. `_CommittedRows` below inserts through
a second, genuinely committed connection (borrowed from the same session-scoped `engine` fixture,
never through `connection`) and deletes whatever it created at teardown, because nothing here rolls
back on its own.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.cli import main
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
    SqlAlchemyExpiredGuestData,
)
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.retention import purge_command
from tailorcraft.infrastructure.retention.heartbeat import HEARTBEAT_KEY, RedisPurgeHeartbeat
from tailorcraft.infrastructure.retention.lock import PURGE_LOCK_KEY
from tailorcraft.infrastructure.settings import Settings

# --- Safety: the one rule that dominates this file -----------------------------------------------


def _assert_test_database(settings: Settings) -> None:
    """Refuse to run a deleting CLI test against anything but `tailorcraft_test`.

    This is the guard CLAUDE.md's 1.4 incident says to write: a script that built its own engine
    from `settings.database_url` under `APP_ENV=test` emptied the **dev** database through these
    very cascades. Every test below that calls `_purge_guests` / `_reclaim_orphans` calls this
    first, on the exact `Settings` object it is about to hand to that seam.
    """
    assert "_test" in settings.database_url, (
        "refusing to run a deleting purge-guests test against a URL that is not the test "
        f"database: {settings.database_url!r}"
    )


# --- Fixtures: logging, and rows committed through a SECOND, real connection ----------------------


@pytest.fixture(autouse=True)
def _configured_logging(settings: Settings) -> None:
    """`_purge_guests`/`_reclaim_orphans` are called directly in this file, never through
    `run_from_cli`, so nothing else in this module calls `configure_logging`. Without it, `caplog`
    would only capture structlog's output if some earlier test in the session happened to configure
    logging first — order-dependent and exactly the kind of thing this file should not lean on."""
    configure_logging(settings)


def _a_past_instant(hours_ago: int) -> datetime:
    """A distinct, whole-second, timezone-aware instant safely in the past. `RetentionWindow.
    expiry_cutoff` returns `clock.now()` unchanged — the window was already applied once, at
    `GuestSession.start`, and is frozen into `expires_at` — so the real adapter's predicate is
    exactly `expires_at <= now()`. Any instant before "now" makes a session expired, regardless of
    `settings.guest_retention_hours`."""
    return datetime.now(UTC).replace(microsecond=0) - timedelta(hours=hours_ago)


@dataclass(slots=True)
class _CommittedRows:
    """Inserts `identity_guest_session` rows through a genuinely committed connection, and deletes
    whatever it created at teardown. See the module docstring for why this cannot be the ordinary
    `session` fixture."""

    engine: AsyncEngine
    _created: list[GuestSessionId] = field(default_factory=list)

    async def insert_expired_guest_session(self, *, expires_at: datetime) -> GuestSessionId:
        session_id = GuestSessionId(uuid4())
        async with self.engine.begin() as conn:
            await conn.execute(
                guest_session_table.insert().values(
                    id=session_id,
                    token_hash=secrets.token_hex(32),
                    created_at=expires_at - timedelta(hours=24),
                    expires_at=expires_at,
                )
            )
        self._created.append(session_id)
        return session_id

    async def session_exists(self, session_id: GuestSessionId) -> bool:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(guest_session_table)
                .where(guest_session_table.c.id == session_id)
            )
            return bool(result.scalar_one() > 0)

    async def cleanup(self) -> None:
        if not self._created:
            return
        async with self.engine.begin() as conn:
            await conn.execute(
                guest_session_table.delete().where(guest_session_table.c.id.in_(self._created))
            )


@pytest_asyncio.fixture
async def committed(engine: AsyncEngine) -> AsyncIterator[_CommittedRows]:
    rows = _CommittedRows(engine=engine)
    try:
        yield rows
    finally:
        await rows.cleanup()


async def _delete_all_currently_expired_sessions(engine: AsyncEngine) -> None:
    """Hygiene for the two empty-backlog tests below: guarantee a clean baseline regardless of what
    an earlier interrupted run in this file (or a previous `make test` invocation) may have left
    committed — `_CommittedRows.cleanup()` only removes what the CURRENT test created. Deletes only
    already-expired rows in `tailorcraft_test`, which is exactly what the purge itself would
    eventually do to them."""
    now = datetime.now(UTC).replace(microsecond=0)
    async with engine.begin() as conn:
        await conn.execute(
            guest_session_table.delete().where(guest_session_table.c.expires_at <= now)
        )


# --- AC-20 / R-1: the run fails, exits 1, and writes no heartbeat ---------------------------------


async def test_exit_1_when_the_database_is_unreachable_and_writes_no_heartbeat(
    settings: Settings,
    clear_redis: None,
) -> None:
    """AC-20's row 1 / R-1. `_purge_batches`'s first statement (`backlog.count()`) fails against an
    unreachable Postgres; the exception is not translated, `_purge_guests` catches it, logs
    `retention.purge_failed` and returns `EXIT_FAILED` — and, per R-1, writes **no** heartbeat: a
    run that died did not finish, and a heartbeat says "finished".

    Mutation target 1's sibling: this is the row AC-20's table calls "the database was unreachable,
    or an unexpected exception escaped", proved against a real (refused) connection rather than a
    double, because the CLI's own composition root — not a use case a test can hand a fake port to
    — is what is under test here.
    """
    _assert_test_database(settings)
    bad_settings = settings.model_copy(
        update={
            "database_url": (
                "postgresql+asyncpg://baduser:badpass@127.0.0.1:1/tailorcraft_test_unreachable"
            )
        }
    )

    exit_code = await purge_command._purge_guests(bad_settings, dry_run=False, limit=None)

    assert exit_code == purge_command.EXIT_FAILED

    redis = create_redis(settings.redis_url)
    try:
        assert await redis.exists(HEARTBEAT_KEY) == 0
    finally:
        await redis.aclose()


# --- AC-20 / R-8: the lock is already held -> exit 3, never 0 -------------------------------------


async def test_exit_3_when_the_lock_is_already_held(
    settings: Settings,
    clear_redis: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-20's row 4, the point of the whole table: a run that did nothing because the lock was
    held must **not** exit 0. Another holder's token is set directly on `PURGE_LOCK_KEY` before the
    call, exactly as a live run (or one still inside its TTL) would leave it.

    Mutation target 1: change `return EXIT_LOCK_HELD` to `return EXIT_OK` in the `if not
    hold.may_run:` branch of `_purge_guests` — this test must go red (`3 == 0` becomes a lie the
    other way, `assert 0 == 3` fails) while every other test in this file stays green.
    """
    _assert_test_database(settings)
    redis = create_redis(settings.redis_url)
    try:
        took_it = await redis.set(PURGE_LOCK_KEY, "someone-elses-token", nx=True, px=30_000)
        assert took_it, "test setup: could not simulate another holder"

        with caplog.at_level(logging.INFO):
            exit_code = await purge_command._purge_guests(settings, dry_run=False, limit=None)

        assert exit_code == purge_command.EXIT_LOCK_HELD
        assert "retention.purge_skipped" in caplog.text
        assert re.search(r'"?reason"?\s*[:=]\s*"?lock_held', caplog.text), caplog.text
        # Nobody ran, so nobody finished: no heartbeat from THIS invocation.
        assert await redis.exists(HEARTBEAT_KEY) == 0
    finally:
        await redis.aclose()


# --- R-7 vs R-8: Redis unavailable is not "the lock is held" --------------------------------------


async def test_redis_unavailable_fails_open_and_the_purge_still_runs(
    settings: Settings,
    committed: _CommittedRows,
) -> None:
    """R-7, and the test that proves it is not R-8 collapsed into one boolean. `RedisPurgeLock.
    acquire` cannot reach an unreachable Redis, returns `UNAVAILABLE`, and `may_run` is true for
    every outcome but `HELD_BY_ANOTHER` — so the purge runs anyway and deletes the real, committed,
    expired session below. Deliberately no `clear_redis` here: this test never reaches the real test
    Redis instance at all, only a `redis_url` pointed at a closed port.

    Contrast with `test_exit_3_when_the_lock_is_already_held` immediately above: same "the lock
    could not be acquired for me" shape at the call site, opposite exit code (0 here, 3 there) and
    opposite effect on the data (deleted here, untouched there). A lock adapter that collapsed
    `UNAVAILABLE` into `HELD_BY_ANOTHER` would make both tests pass with the same wrong exit code;
    they only discriminate together.
    """
    _assert_test_database(settings)
    session_id = await committed.insert_expired_guest_session(expires_at=_a_past_instant(2))
    bad_redis_settings = settings.model_copy(update={"redis_url": "redis://127.0.0.1:1/0"})

    exit_code = await purge_command._purge_guests(bad_redis_settings, dry_run=False, limit=None)

    assert exit_code == purge_command.EXIT_OK
    assert not await committed.session_exists(session_id)


# --- AC-20 / R-14: a run over an empty backlog is still a completed run ---------------------------


async def test_exit_0_on_a_run_with_no_backlog_and_writes_the_heartbeat(
    settings: Settings,
    engine: AsyncEngine,
    clear_redis: None,
) -> None:
    """AC-20's row "success (including a run that deleted nothing)" and R-14: a do-nothing run is
    still a completed one, so it still writes the heartbeat. Read back directly from Redis through
    `RedisPurgeHeartbeat.read`, never inferred from the CLI's own stdout report.

    Mutation target 3's neighbour: if `record(...)` were skipped for a zero-deletion run, this test
    would fail on `heartbeat is not None` while the AC-18/AC-19 tests (which always delete
    something) would still pass — proving this specific test is the one guarding the empty case.
    """
    _assert_test_database(settings)
    await _delete_all_currently_expired_sessions(engine)

    exit_code = await purge_command._purge_guests(settings, dry_run=False, limit=None)

    assert exit_code == purge_command.EXIT_OK

    redis = create_redis(settings.redis_url)
    try:
        heartbeat = await RedisPurgeHeartbeat(redis).read()
    finally:
        await redis.aclose()

    assert heartbeat is not None
    assert heartbeat.outcome == "ok"
    assert heartbeat.sessions_deleted == 0
    assert heartbeat.files_unlinked == 0


async def test_purge_completed_log_line_is_emitted_on_an_empty_backlog_with_its_full_field_set(
    settings: Settings,
    engine: AsyncEngine,
    clear_redis: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-21 / R-14: the line a job whose failure mode is silence needs — emitted on **every**
    completed run, including the ones that delete nothing — with the task's full eight-field set.

    Mutation target 2: delete the `_log_purge_line(run, ...)` call from the success path of
    `_purge_guests` — this test must go red (`"retention.purge_completed" in caplog.text` becomes
    false) while `test_exit_0_on_a_run_with_no_backlog_and_writes_the_heartbeat` above, which never
    reads `caplog`, keeps passing. That is exactly the gap AC-21 exists to close: a heartbeat alone
    is not "one structured log line on every run".
    """
    _assert_test_database(settings)
    await _delete_all_currently_expired_sessions(engine)

    with caplog.at_level(logging.INFO):
        exit_code = await purge_command._purge_guests(settings, dry_run=False, limit=None)

    assert exit_code == purge_command.EXIT_OK
    assert "retention.purge_completed" in caplog.text

    for expected_field in (
        "sessions_deleted",
        "sessions_failed",
        "files_unlinked",
        "files_failed",
        "examined",
        "overdue_after",
        "dry_run",
        "duration_ms",
    ):
        assert re.search(rf'"?{expected_field}"?\s*[:=]', caplog.text), caplog.text
    assert re.search(r'"?sessions_deleted"?\s*[:=]\s*0\b', caplog.text), caplog.text
    assert re.search(r'"?dry_run"?\s*[:=]\s*(false|False)\b', caplog.text), caplog.text


# --- AC-17 / R-13: a dry run writes nothing of any kind --------------------------------------------


async def test_dry_run_performs_no_write_of_any_kind(
    settings: Settings,
    committed: _CommittedRows,
    clear_redis: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-17 / R-13. The absence of calls is the acceptance criterion, so both mutating ports are
    monkeypatched to **spies** on the real classes for the duration of this one test — a runtime
    patch, not a change to `infrastructure/`'s own source — and the assertion is that the spy lists
    stay empty, never that the report's counters happen to read zero (a buggy implementation could
    call and discard a result and still report zero). Confirmed independently: the row survives a
    direct query, and neither `PURGE_LOCK_KEY` nor `HEARTBEAT_KEY` exists in Redis afterward.

    Mutation target 3: make the dry-run path call `RedisPurgeHeartbeat(...).record(...)` — this test
    must go red on the `redis.exists(HEARTBEAT_KEY) == 0` assertion.
    """
    _assert_test_database(settings)
    session_id = await committed.insert_expired_guest_session(expires_at=_a_past_instant(2))

    delete_session_calls: list[GuestSessionId] = []
    file_delete_calls: list[FileRef] = []

    async def _spy_delete_session(self: SqlAlchemyExpiredGuestData, sid: GuestSessionId) -> None:
        delete_session_calls.append(sid)

    async def _spy_file_delete(self: LocalFileStore, ref: FileRef) -> None:
        file_delete_calls.append(ref)

    monkeypatch.setattr(SqlAlchemyExpiredGuestData, "delete_session", _spy_delete_session)
    monkeypatch.setattr(LocalFileStore, "delete", _spy_file_delete)

    exit_code = await purge_command._purge_guests(settings, dry_run=True, limit=None)

    assert exit_code == purge_command.EXIT_OK
    assert delete_session_calls == []
    assert file_delete_calls == []
    assert await committed.session_exists(session_id)

    redis = create_redis(settings.redis_url)
    try:
        assert await redis.exists(PURGE_LOCK_KEY) == 0
        assert await redis.exists(HEARTBEAT_KEY) == 0
    finally:
        await redis.aclose()

    out = capsys.readouterr().out
    assert "Nothing was deleted." in out


# --- AC-18 / R-12: --limit smaller than the backlog -------------------------------------------------


async def test_limit_smaller_than_the_backlog_deletes_at_most_n_sessions_and_exits_0(
    settings: Settings,
    committed: _CommittedRows,
    clear_redis: None,
) -> None:
    """AC-18 / R-12. Three real, committed, expired sessions; `--limit 2` must take exactly the two
    oldest (the port's `ORDER BY expires_at ASC` promise) and leave the newest in place, in exactly
    one batch, exiting 0. `--limit` smaller than the backlog is a normal outcome, never an error.

    Mutation target 4: make `--limit` ignored (drop the `limit is not None` half of `_purge_batches`'s
    loop-stop condition, so it keeps batching past N) — this test must go red on
    `session_exists(newest) is True` turning false.
    """
    _assert_test_database(settings)
    oldest = await committed.insert_expired_guest_session(expires_at=_a_past_instant(3))
    middle = await committed.insert_expired_guest_session(expires_at=_a_past_instant(2))
    newest = await committed.insert_expired_guest_session(expires_at=_a_past_instant(1))

    exit_code = await purge_command._purge_guests(settings, dry_run=False, limit=2)

    assert exit_code == purge_command.EXIT_OK
    assert not await committed.session_exists(oldest)
    assert not await committed.session_exists(middle)
    assert await committed.session_exists(newest)


# --- AC-19: no --limit loops batches until the backlog is empty ------------------------------------


async def test_no_limit_loops_batches_until_the_backlog_is_empty(
    settings: Settings,
    committed: _CommittedRows,
    clear_redis: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-19. `_DEFAULT_BATCH_LIMIT` is monkeypatched down to 2 so the test can prove looping with
    five rows instead of the module's real default of 100 — five real, committed, expired sessions,
    all deleted with no `--limit` given, across batches of 2, 2 and 1. The loop's stop condition is
    `sessions_deleted == 0` (`_purge_batches`'s docstring), not "the backlog is empty", so a fourth,
    empty batch is expected too — that is the termination check running, not a bug. The batch count
    is read from the CLI's own per-batch progress lines (`_print_batch_line`) as an independent check
    that looping actually happened, rather than a single oversized batch coincidentally clearing five
    rows in one pass.
    """
    _assert_test_database(settings)
    monkeypatch.setattr(purge_command, "_DEFAULT_BATCH_LIMIT", 2)
    ids = [
        await committed.insert_expired_guest_session(expires_at=_a_past_instant(h))
        for h in (5, 4, 3, 2, 1)
    ]

    exit_code = await purge_command._purge_guests(settings, dry_run=False, limit=None)

    assert exit_code == purge_command.EXIT_OK
    for session_id in ids:
        assert not await committed.session_exists(session_id)

    out = capsys.readouterr().out
    batch_lines = re.findall(r"^  batch \d+:", out, flags=re.MULTILINE)
    assert len(batch_lines) == 4, out  # 2 + 2 + 1, plus the empty batch that stops the loop


# --- AC-20: argparse's own usage exit, exercised through the real parser --------------------------


def test_usage_error_exits_2() -> None:
    """AC-20's row 3. `--limit 0` is refused by `_positive_int`'s `ArgumentTypeError`, which
    argparse turns into `SystemExit(2)` before `purge-guests` ever runs — no database, no Redis, the
    real `cli.main` end to end."""
    with pytest.raises(SystemExit) as exc_info:
        main(["purge-guests", "--limit", "0"])
    assert exc_info.value.code == 2
