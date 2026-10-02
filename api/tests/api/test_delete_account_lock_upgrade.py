"""Slice 2.5, T14 regression: two concurrent deletions of one account, staged deterministically.

2.2's S-46 (`test_auth.py::test_two_concurrent_correct_deletions_exactly_one_204_one_401`) says
two correct-password deletions of one account answer exactly one 204 and one 401
`not_signed_in` ("user gone"). T14 added `users.confirm_credential_unchanged` to
`DeleteOwnAccount`: a `SELECT … FOR SHARE` on the user row, held to the end of the transaction,
followed by erasure's `SELECT … FOR UPDATE` on the same row — a lock **upgrade**. That breaks S-46
two ways, and the existing test sees them only when the scheduler happens to line up:

(a) both requests hold `FOR SHARE`, both then ask for `FOR UPDATE` -> `deadlock_detected`, one is
    aborted -> 503 instead of 204/401;
(b) the winner commits before the loser's re-check -> the re-check finds no row -> `False` ->
    `InvalidCredentials` -> 403 `password_incorrect` instead of 401 (the user is gone).

Each interleaving is staged by wrapping `SqlAlchemyUserRepository.confirm_credential_unchanged` —
the single seam between "the password verified" and "the erasure starts", so the wrapper decides
the order and the code under test is otherwise untouched. `SET LOCAL lock_timeout` is issued on the
request's own transaction (it lasts exactly as long as the locks it guards, so it cannot be lost on
a pooled connection), so a regression that waits for ever fails in seconds naming a lock.

Real, committed rows on `tailorcraft_test`, cleaned up by hand. The expectation is the spec's, not
the code's: exactly one 204, exactly one 401 `not_signed_in`, never a 403, never a 503.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.domain.identity.value_objects import PasswordHash, UserId
from tailorcraft.infrastructure.api.main import create_app
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.database import create_session_factory
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks.app import app as celery_app
from tests.integration.claim_race_support import wait_for_lock_waiter

REGISTER_URL = "/api/auth/register"
DELETE_ACCOUNT_URL = "/api/auth/delete-account"
A_STRONG_PASSWORD = "correct horse battery staple 9"
LOCK_TIMEOUT_MS = 8_000
GATE_SECONDS = 10.0

_Confirm = Callable[[SqlAlchemyUserRepository, UserId, PasswordHash], Awaitable[bool]]


@pytest.fixture
def concurrent_app(
    settings: Settings, engine: AsyncEngine, password_hasher: Argon2PasswordHasher
) -> FastAPI:
    """A real session per request (see `test_auth.py`'s fixture of the same name for why)."""
    app = create_app(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.celery = celery_app
    app.state.password_hasher = password_hasher
    return app


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """The delete route and register touch rate limiters; Redis is not rolled back with Postgres."""


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
    )


def _assert_test_database(settings: Settings) -> None:
    assert "_test" in settings.database_url, (
        f"refusing to run a deleting test against {settings.database_url!r}"
    )


async def _register(app: FastAPI, settings: Settings) -> tuple[str, UUID]:
    async with _client(app) as client:
        response = await client.post(
            "/api/auth/register",
            json={"email": f"t14-lock-{uuid4().hex}@example.com", "password": A_STRONG_PASSWORD},
            headers={"Origin": settings.public_base_url},
        )
    assert response.status_code == 201, response.text
    body = response.json()
    return str(body["access_token"]), UUID(str(body["user"]["id"]))


async def _user_row_exists(engine: AsyncEngine, user_id: UUID) -> bool:
    async with engine.connect() as conn:
        count = await conn.execute(
            text("SELECT count(*) FROM identity_user WHERE id = :i"), {"i": user_id}
        )
        return bool(count.scalar_one())


async def _delete_twice(app: FastAPI, settings: Settings, token: str) -> tuple[Response, Response]:
    headers = {"Origin": settings.public_base_url, "Authorization": f"Bearer {token}"}
    async with _client(app) as a, _client(app) as b:
        first, second = await asyncio.gather(
            a.post(DELETE_ACCOUNT_URL, json={"password": A_STRONG_PASSWORD}, headers=headers),
            b.post(DELETE_ACCOUNT_URL, json={"password": A_STRONG_PASSWORD}, headers=headers),
        )
    return first, second


def _assert_one_204_one_401_user_gone(results: tuple[Response, Response]) -> None:
    statuses = sorted(r.status_code for r in results)
    assert statuses == [204, 401], [(r.status_code, r.text) for r in results]
    the_401 = next(r for r in results if r.status_code == 401)
    assert the_401.json()["error"]["code"] == "not_signed_in", the_401.text


def _install(monkeypatch: pytest.MonkeyPatch, staged: _Confirm) -> None:
    """Replace the seam with `staged`, which receives the **real** method as `real` via closure of
    the caller; each wrapper first bounds lock waits on the request's own transaction."""
    monkeypatch.setattr(SqlAlchemyUserRepository, "confirm_credential_unchanged", staged)


async def _set_lock_timeout(repo: SqlAlchemyUserRepository) -> None:
    session: Any = (
        repo._session
    )  # the request's own session; `SET LOCAL` lives as long as its locks
    await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'"))


async def test_two_deletions_both_holding_the_shared_lock_do_not_deadlock(
    concurrent_app: FastAPI,
    settings: Settings,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interleaving (a): neither request may reach erasure until the other has finished its re-check
    or is parked on the row's lock. Under today's `FOR SHARE` re-check both then hold a shared lock and both ask for
    `FOR UPDATE`: a lock upgrade, so PostgreSQL aborts one with `deadlock_detected`. The spec's
    answer is one 204 and one 401 whatever lock the fix chooses. A fix that serialises the
    re-checks makes the second wait on the lock, which the gate tolerates."""
    _assert_test_database(settings)
    real = SqlAlchemyUserRepository.confirm_credential_unchanged
    rechecked = 0
    both_rechecked = asyncio.Event()
    both_held_the_lock = False

    async def staged(repo: SqlAlchemyUserRepository, user_id: UserId, seen: PasswordHash) -> bool:
        nonlocal rechecked, both_held_the_lock
        await _set_lock_timeout(repo)
        answer = await real(repo, user_id, seen)
        rechecked += 1
        if rechecked == 2:
            both_held_the_lock = True
            both_rechecked.set()
        # Hold the re-check's lock until the other request has either finished *its* re-check
        # (both hold `FOR SHARE`: today's code) or is observably parked on this row's lock (a fix
        # that takes an exclusive lock early, which this gate must not hang on).
        waiter = asyncio.ensure_future(wait_for_lock_waiter(engine, "identity_user"))
        done = asyncio.ensure_future(both_rechecked.wait())
        try:
            await asyncio.wait(
                {waiter, done}, timeout=GATE_SECONDS, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (waiter, done):
                task.cancel()
            await asyncio.gather(waiter, done, return_exceptions=True)
        return answer

    token, user_id = await _register(concurrent_app, settings)
    _install(monkeypatch, staged)
    try:
        results = await _delete_twice(concurrent_app, settings, token)
        assert both_held_the_lock or rechecked == 1, "the re-checks never overlapped: not staged"
        _assert_one_204_one_401_user_gone(results)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id})


async def test_a_deletion_whose_account_was_erased_before_its_recheck_is_401_not_403(
    concurrent_app: FastAPI,
    settings: Settings,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interleaving (b): the first request to reach the re-check proceeds untouched and erases the
    account; the second is held **before** its re-check until the user row is observably gone on a
    third connection (so the winner has committed). It already verified the password against a
    row that existed. S-46: that is "user gone" -> 401 `not_signed_in`, never 403
    `password_incorrect` — the password was right, the account was erased."""
    _assert_test_database(settings)
    real = SqlAlchemyUserRepository.confirm_credential_unchanged
    arrivals = 0
    loser_saw_row_gone = False

    async def staged(repo: SqlAlchemyUserRepository, user_id: UserId, seen: PasswordHash) -> bool:
        nonlocal arrivals, loser_saw_row_gone
        await _set_lock_timeout(repo)
        arrivals += 1  # synchronous: the first to arrive is the winner
        if arrivals == 1:
            return await real(repo, user_id, seen)
        deadline = asyncio.get_running_loop().time() + GATE_SECONDS
        while await _user_row_exists(engine, user_id.value):
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(0.02)
        loser_saw_row_gone = not await _user_row_exists(engine, user_id.value)
        return await real(repo, user_id, seen)

    token, user_id = await _register(concurrent_app, settings)
    _install(monkeypatch, staged)
    try:
        results = await _delete_twice(concurrent_app, settings, token)
        assert arrivals == 2, "both requests must reach the re-check"
        assert loser_saw_row_gone, (
            "the loser's re-check ran before the winner committed: not staged"
        )
        _assert_one_204_one_401_user_gone(results)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id})
