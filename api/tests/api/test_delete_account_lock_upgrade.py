"""Slice 2.5, T14 regression: two concurrent deletions of one account, staged deterministically.

2.2's S-46 (`test_auth.py::test_two_concurrent_correct_deletions_exactly_one_204_one_401`) says
two correct-password deletions of one account answer exactly one 204 and one 401
`not_signed_in` ("user gone"). The credential re-check between "the password verified" and
"erasure starts" must not break that. T14's first shape (`confirm_credential_unchanged`, a
`SELECT ... FOR SHARE` held to the end of the transaction, then erasure's `FOR UPDATE` on the same
row) was a lock **upgrade** and broke S-46 two ways:

(a) both requests hold `FOR SHARE`, both then ask for `FOR UPDATE` -> `deadlock_detected`, one is
    aborted -> 503 instead of 204/401;
(b) the winner commits before the loser's re-check -> the re-check finds no row -> `False` ->
    `InvalidCredentials` -> 403 `password_incorrect` instead of 401 (the user is gone).

Amended AC-14 (ADR-0028 section 5): `DeleteOwnAccount` re-checks with `users.get_for_update`
(`FOR UPDATE`), so a second deletion waits and then finds the row gone. This test does not know
which lock the fix chooses. It stages each interleaving at the **point between verify and erasure**
by wrapping every re-check entry point on `SqlAlchemyUserRepository` -- `confirm_credential_unchanged`
(the old shape) and `get_for_update` (the new one); a request calls exactly one of them once on the
deletion path, so the wrapper runs a gate immediately before whichever lock the code under test
takes first. The wrappers decide the order; the code under test is otherwise untouched.
`SET LOCAL lock_timeout` is issued on the request's own transaction (it lasts exactly as long as
the locks it guards, so it cannot be lost on a pooled connection), so a regression that waits for
ever fails in seconds naming a lock.

Real, committed rows on `tailorcraft_test`, cleaned up by hand. The expectation is the spec's, not
the code's: exactly one 204, exactly one 401 `not_signed_in`, never a 403, never a 503.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.infrastructure.api.main import create_app
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.database import create_session_factory
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks.app import app as celery_app
from tests.api.me_support import seed_user_and_sign_in

DELETE_ACCOUNT_URL = "/api/auth/delete-account"
A_STRONG_PASSWORD = "correct horse battery staple 9"
LOCK_TIMEOUT_MS = 8_000
GATE_SECONDS = 10.0

_Real = Callable[..., Awaitable[Any]]


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
    """A committed user, signed in through the real login route. Slice 2.5 (T27): `POST
    /api/auth/register` no longer creates an account or a token (202, empty), so this seeds the user
    through the repository (`me_support.seed_user_and_sign_in`, T7) — the lock-upgrade race under test
    starts from "an account with a bearer", not from how it got one."""
    async with _client(app) as client:
        return await seed_user_and_sign_in(
            client, settings, email=f"t14-lock-{uuid4().hex}@example.com"
        )


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


async def _set_lock_timeout(repo: SqlAlchemyUserRepository) -> None:
    session: Any = (
        repo._session
    )  # the request's own session; `SET LOCAL` lives as long as its locks
    await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'"))


def _install(
    monkeypatch: pytest.MonkeyPatch,
    gate: Callable[[SqlAlchemyUserRepository], Awaitable[None]],
) -> None:
    """Run `gate(repo)` right before the first re-check lock a deletion takes, whichever method that
    is. Both entry points are wrapped; the real method then runs unchanged."""

    def wrap(real: _Real) -> _Real:
        async def staged(repo: SqlAlchemyUserRepository, *args: Any, **kwargs: Any) -> Any:
            await _set_lock_timeout(repo)
            await gate(repo)
            return await real(repo, *args, **kwargs)

        return staged

    for name in ("confirm_credential_unchanged", "get_for_update"):
        monkeypatch.setattr(
            SqlAlchemyUserRepository, name, wrap(getattr(SqlAlchemyUserRepository, name))
        )


async def test_two_concurrent_correct_deletions_do_not_deadlock(
    concurrent_app: FastAPI,
    settings: Settings,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interleaving (a): neither request takes its re-check lock until **both** have passed the
    password verify, so the two genuinely overlap at the lock. Under the old `FOR SHARE` re-check
    both then hold a shared lock and both ask for `FOR UPDATE`: a lock upgrade, so PostgreSQL
    aborts one with `deadlock_detected` (503). With an exclusive re-check the second simply waits
    for the first to commit and then finds the row gone. The spec's answer is one 204 and one 401
    whatever lock the fix chooses."""
    _assert_test_database(settings)
    arrived = 0
    both_verified = asyncio.Event()
    overlapped = False

    async def gate(repo: SqlAlchemyUserRepository) -> None:
        nonlocal arrived, overlapped
        arrived += 1  # synchronous: no await between the read and the write
        if arrived == 2:
            overlapped = True
            both_verified.set()
        # On timeout `overlapped` stays False and the assertion below names it.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(both_verified.wait(), timeout=GATE_SECONDS)

    token, user_id = await _register(concurrent_app, settings)
    _install(monkeypatch, gate)
    try:
        results = await _delete_twice(concurrent_app, settings, token)
        assert overlapped, "the two requests never both reached the re-check: not staged"
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
    `password_incorrect` -- the password was right, the account was erased."""
    _assert_test_database(settings)
    user_id_holder: list[UUID] = []
    arrivals = 0
    loser_saw_row_gone = False

    async def gate(repo: SqlAlchemyUserRepository) -> None:
        nonlocal arrivals, loser_saw_row_gone
        arrivals += 1  # synchronous: the first to arrive is the winner
        if arrivals == 1:
            return
        target = user_id_holder[0]
        deadline = asyncio.get_running_loop().time() + GATE_SECONDS
        while await _user_row_exists(engine, target):
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(0.02)
        loser_saw_row_gone = not await _user_row_exists(engine, target)

    token, user_id = await _register(concurrent_app, settings)
    user_id_holder.append(user_id)
    _install(monkeypatch, gate)
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
