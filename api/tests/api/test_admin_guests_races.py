"""Slice 4.4's races, over HTTP on a concurrent app (a real session per request), T17: F-7, F-8,
F-17, plus the directory's `EXPLAIN` (AC-9, technical plan §3).

Each race is staged **at the moment the spec names**, not before the request (H-33): a class-level
wrapper over the real `SqlAlchemyExpiredGuestData` method runs the *other* actor's real HTTP
request inside the first request's window, and records the order. Waits are `wait_for`-bounded
(a hung test is worse than a failing one) and an overlap is proven from `pg_stat_activity` with
`wait_for_lock_waiter`, never from a sleep.

- **F-7a** claim between the erase's `session_with_files` read and its `delete_session`: the erase
  is 404, the (now user's) files are kept, the rows are the user's.
- **F-7b** erase first: the claim waits on the erase's uncommitted `DELETE` (observed), then answers
  200 with zeros; the erase unlinked the files.
- **F-8** purge and erase of one expired session, both interleavings: the loser's `DELETE` removes
  nothing — purge `sessions_skipped = 1` / erase 404 — and the files are unlinked exactly once.
- **F-17** two purge runs: the second, arriving while the first holds the lock, is 409.

**Mutation 4 (T9's, re-run against F-7a), restored byte-exact:** `purge_one` unlinks the candidate's
files even when `delete_session` answered `False`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import Response
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tailorcraft.domain.intake.value_objects import CvContentType
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.queries.guest_session_directory import (
    SqlGuestSessionDirectory,
)
from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
    SqlAlchemyExpiredGuestData,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.admin_guest_world import Guest, World, open_world
from tests.api.me_support import error_code
from tests.integration.claim_race_support import wait_for_lock_waiter

GUESTS = "/api/admin/guest-sessions"
PURGE = "/api/admin/guest-purge"
CLAIM = "/api/me/guest-work/claim"
_STEP = 20.0


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """The purge lock and the claim limiter live in Redis, which no rollback reaches."""


@pytest_asyncio.fixture
async def world(
    settings: Settings,
    engine: AsyncEngine,
    password_hasher: Argon2PasswordHasher,
    tmp_path: Path,
) -> AsyncIterator[World]:
    async with open_world(
        settings, engine, password_hasher, tmp_path / "uploads", clean_guest_sessions=True
    ) as w:
        yield w


def _on_volume(world: World, refs: list[FileRef]) -> list[bool]:
    return [world.path(r).is_file() for r in refs]


async def _guest_with_files(world: World, **kwargs: Any) -> tuple[Guest, list[FileRef]]:
    guest = await world.guest(**kwargs)
    entry = await world.entry(guest.owner)
    return guest, [FileRef.for_base_cv(entry.cv_id, CvContentType.PDF), entry.jobs[0].storage_ref]


def _after(
    method: str, hook: Callable[[], Awaitable[None]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run `hook` once, right after the real `SqlAlchemyExpiredGuestData.<method>` returned."""
    real = getattr(SqlAlchemyExpiredGuestData, method)
    fired = False

    async def wrapper(self: SqlAlchemyExpiredGuestData, *args: Any, **kwargs: Any) -> Any:
        nonlocal fired
        result = await real(self, *args, **kwargs)
        if not fired:
            fired = True
            await asyncio.wait_for(hook(), _STEP)
        return result

    monkeypatch.setattr(SqlAlchemyExpiredGuestData, method, wrapper)


# ------------------------------------------------------------------------------- F-7


async def test_f7_a_claim_between_the_erases_read_and_delete_makes_it_404_and_keeps_the_files(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin, user = await world.account(admin=True), await world.account()
    guest, refs = await _guest_with_files(world)
    claimer = world.guest_client(guest)
    claimed: list[Response] = []

    async def claim_now() -> None:
        claimed.append(await claimer.post(CLAIM, headers=user.headers))

    _after("session_with_files", claim_now, monkeypatch)

    response = await asyncio.wait_for(
        world.http.delete(f"{GUESTS}/{guest.uuid}", headers=admin.headers), _STEP
    )

    assert [c.status_code for c in claimed] == [200], "premise: the claim ran mid-erase"
    assert claimed[0].json()["base_cvs"] == 1
    assert response.status_code == 404, response.text
    assert error_code(response) == "guest_session_not_found"
    assert _on_volume(world, refs) == [True, True], "the user's files were unlinked by the erase"
    assert (await world.rows_of_user(user))["intake_base_cv"] == 1
    assert (await world.rows_of_guest(guest))["identity_guest_session"] == 0


async def test_f7_an_erase_that_commits_first_makes_the_waiting_claim_200_with_zeros(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin, user = await world.account(admin=True), await world.account()
    guest, refs = await _guest_with_files(world)
    claimer = world.guest_client(guest)
    deleted, release = asyncio.Event(), asyncio.Event()
    real = SqlAlchemyExpiredGuestData.delete_session

    async def held(self: SqlAlchemyExpiredGuestData, session_id: Any) -> bool:
        result = await real(self, session_id)  # the row lock is held, uncommitted, from here
        deleted.set()
        await asyncio.wait_for(release.wait(), _STEP)
        return result

    monkeypatch.setattr(SqlAlchemyExpiredGuestData, "delete_session", held)
    erase = asyncio.create_task(world.http.delete(f"{GUESTS}/{guest.uuid}", headers=admin.headers))
    try:
        await asyncio.wait_for(deleted.wait(), _STEP)
        claim = asyncio.create_task(claimer.post(CLAIM, headers=user.headers))
        try:
            assert await wait_for_lock_waiter(world.engine, "identity_guest_session", "for update")
            assert not claim.done(), "the claim answered while the erase held the row"
        finally:
            release.set()
        erased = await asyncio.wait_for(erase, _STEP)
        claimed = await asyncio.wait_for(claim, _STEP)
    finally:
        release.set()
        erase.cancel()

    assert erased.status_code == 200, erased.text
    assert claimed.status_code == 200, claimed.text
    assert {v for k, v in claimed.json().items() if isinstance(v, int)} == {0}, claimed.json()
    assert _on_volume(world, refs) == [False, False]
    assert (await world.rows_of_user(user))["intake_base_cv"] == 0


# ------------------------------------------------------------------------------- F-8


async def test_f8_a_purge_that_wins_between_the_erases_read_and_delete_makes_the_erase_404(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await world.account(admin=True)
    guest, refs = await _guest_with_files(world, started=timedelta(hours=30))
    purged: list[Response] = []

    async def purge_now() -> None:
        purged.append(await world.http.post(PURGE, json={}, headers=admin.headers))

    _after("session_with_files", purge_now, monkeypatch)

    response = await asyncio.wait_for(
        world.http.delete(f"{GUESTS}/{guest.uuid}", headers=admin.headers), _STEP
    )

    assert purged[0].status_code == 200, purged[0].text
    assert purged[0].json()["sessions_deleted"] == 1
    assert response.status_code == 404, response.text
    assert error_code(response) == "guest_session_not_found"
    assert _on_volume(world, refs) == [False, False], "the purge unlinks them, once"


async def test_f8_an_erase_that_wins_between_the_purges_listing_and_delete_makes_it_skip(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await world.account(admin=True)
    guest, refs = await _guest_with_files(world, started=timedelta(hours=30))
    erased: list[Response] = []

    async def erase_now() -> None:
        erased.append(await world.http.delete(f"{GUESTS}/{guest.uuid}", headers=admin.headers))

    _after("list_expired", erase_now, monkeypatch)

    response = await asyncio.wait_for(world.http.post(PURGE, json={}, headers=admin.headers), _STEP)

    assert erased[0].status_code == 200, erased[0].text
    assert erased[0].json()["files_unlinked"] == 2
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["examined"], body["sessions_skipped"], body["sessions_deleted"]) == (1, 1, 0)
    assert body["files_unlinked"] == 0, "the purge unlinked files it did not delete the row of"
    assert _on_volume(world, refs) == [False, False]


# ------------------------------------------------------------------------------- F-17


async def test_f17_a_second_purge_while_the_first_runs_is_409_and_the_first_still_completes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await world.account(admin=True)
    _guest, refs = await _guest_with_files(world, started=timedelta(hours=30))
    listed, release = asyncio.Event(), asyncio.Event()
    real = SqlAlchemyExpiredGuestData.list_expired

    async def held(self: SqlAlchemyExpiredGuestData, *args: Any, **kwargs: Any) -> Any:
        result = await real(self, *args, **kwargs)
        listed.set()
        await asyncio.wait_for(release.wait(), _STEP)
        return result

    monkeypatch.setattr(SqlAlchemyExpiredGuestData, "list_expired", held)
    first = asyncio.create_task(world.http.post(PURGE, json={}, headers=admin.headers))
    try:
        await asyncio.wait_for(listed.wait(), _STEP)
        second = await asyncio.wait_for(
            world.http.post(PURGE, json={}, headers=admin.headers), _STEP
        )
        release.set()
        done = await asyncio.wait_for(first, _STEP)
    finally:
        release.set()
        first.cancel()

    assert second.status_code == 409, second.text
    assert error_code(second) == "purge_in_progress"
    assert done.status_code == 200, done.text
    assert done.json()["sessions_deleted"] == 1
    assert _on_volume(world, refs) == [False, False]


# ------------------------------------------------------------------------------- EXPLAIN


async def test_the_directory_page_is_served_by_the_expires_at_index(
    engine: AsyncEngine, settings: Settings
) -> None:
    """Other rows seeded so the predicate is selective (3.1's lesson): 5 000 expired sessions and a
    few live ones, `ANALYZE`d, in a transaction that is rolled back. The statement is the one
    `page()` really sends, captured at the cursor, not a copy of it."""
    assert "_test" in settings.database_url
    now = SystemClock().now()
    captured: list[tuple[str, Any]] = []

    def capture(conn: Any, cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        if "FROM identity_guest_session" in statement and "count(*)" in statement:
            captured.append((statement, parameters))

    async with engine.connect() as conn:
        await conn.execute(text("SET LOCAL lock_timeout = '8s'"))
        await conn.execute(
            text(
                "INSERT INTO identity_guest_session (id, token_hash, created_at, expires_at) "
                "SELECT gen_random_uuid(), md5(g::text) || md5(g::text || 'x'), CAST(:c AS timestamptz), "
                "CAST(:e AS timestamptz) - make_interval(secs => g) FROM generate_series(1, 5000) g"
            ),
            {"c": now - timedelta(days=9), "e": now - timedelta(hours=1)},
        )
        await conn.execute(
            text(
                "INSERT INTO identity_guest_session (id, token_hash, created_at, expires_at) "
                "SELECT gen_random_uuid(), md5(g::text || 'live') || md5(g::text || 'y'), CAST(:c AS timestamptz), "
                "CAST(:e AS timestamptz) + make_interval(secs => g) FROM generate_series(1, 3) g"
            ),
            {"c": now, "e": now + timedelta(hours=1)},
        )
        await conn.execute(text("ANALYZE identity_guest_session"))
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        try:
            page = await SqlGuestSessionDirectory(AsyncSession(bind=conn)).page(now, None, 20)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
        assert len(page.items) >= 3, "premise: the live rows are in the page"
        assert len(captured) == 1, captured
        statement, parameters = captured[0]
        plan = "\n".join(
            row[0]
            for row in (await conn.exec_driver_sql("EXPLAIN " + statement, tuple(parameters))).all()
        )
        await conn.rollback()

    top = plan.split("SubPlan")[0]
    assert "ix_identity_guest_session_expires_at" in top, plan
    assert "Seq Scan on identity_guest_session" not in top, plan
