# ruff: noqa: F811
"""Slice 4.2, T17 (test-after): AC-30 (a)-(f), the account actions racing each other and the
credential flows, on a concurrent app (a real session and connection per request).

**How each race is staged, and how the overlap is proven.** A blocker connection takes a row lock the
actions need (`SELECT … FOR UPDATE` on the target's user row, or on its login rows for (f)). The
racers are fired; the test polls `pg_stat_activity` until the expected number of *other* backends
show `wait_event_type = 'Lock'`, which is the proof that the requests were in flight together and
queued behind the same row. Only then is the blocker released, so what the database does next is the
serialization under test. Every wait has a timeout, the blocker is released in a `finally`, and the
cleanup sets a `lock_timeout`: a race test that hangs is worse than one that fails.

**Mutation 3, observed 2026-10-11 and restored byte-exact** (`git diff --stat -- api/src` empty):
`remove_all_for_user` moved ahead of `get_for_update` in `DisableAccount` -> 2 red, (c)
`assert 503 in (200, 404)` and (e) `assert 503 == 200`: the lock-order inversion deadlocks against
erase and against reset-confirm (`db.request_failed DBAPIError`, answered 503).

Final states are read on a fresh `engine.connect()`. Responses are checked for truthfulness against
that final state, since the order in which two waiters are woken is the database's to choose.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container
from tests.api.account_mail_support import install_recording_queue, reset_token
from tests.api.me_support import A_PASSWORD, error_code
from tests.api.test_admin_users import (  # noqa: F401
    USERS,
    _reset_redis_between_tests,
    http,
    make_account,
    seeded_ids,
    world_app,
)
from tests.integration.fakes import RecordingAccountMailer

_WAIT = 15.0
A_NEW_PASSWORD = "an entirely different passphrase 7"
RESET = "/api/auth/password-reset"
RESET_CONFIRM = "/api/auth/password-reset/confirm"
REFRESH = "/api/auth/refresh"
LOGIN = "/api/auth/login"


async def _waiters(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                    )
                )
            ).scalar_one()
        )


async def _race(
    engine: AsyncEngine, lock_sql: str, params: dict[str, object], *racers: Awaitable[Response]
) -> tuple[list[Response], int]:
    """Fire `racers` while a blocker holds `lock_sql`'s rows; release once all are seen waiting.
    Returns the responses and the number of waiters observed (the overlap proof)."""
    blocker = await engine.connect()
    tasks: list[asyncio.Task[Response]] = []
    observed = 0
    try:
        await blocker.execute(text("SET lock_timeout = '0'"))
        await blocker.execute(text(lock_sql), params)
        tasks = [asyncio.create_task(asyncio.wait_for(r, _WAIT * 2)) for r in racers]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _WAIT
        while True:
            observed = await _waiters(engine)
            if observed >= len(racers):
                break
            if any(t.done() for t in tasks):
                done = [t.result() for t in tasks if t.done()]
                raise AssertionError(f"a racer finished before the overlap was staged: {done}")
            if loop.time() > deadline:
                raise AssertionError(f"only {observed} of {len(racers)} requests queued")
            await asyncio.sleep(0.02)
    finally:
        await blocker.rollback()
        await blocker.close()
    try:
        return list(await asyncio.wait_for(asyncio.gather(*tasks), _WAIT * 2)), observed
    finally:
        for t in tasks:
            t.cancel()


_USER_LOCK = "SELECT 1 FROM identity_user WHERE id = :t FOR UPDATE"
_LOGIN_LOCK = "SELECT 1 FROM identity_login WHERE user_id = :t FOR UPDATE"


async def _state(engine: AsyncEngine, user_id: UUID) -> tuple[int, object, int]:
    """(user rows, disabled_at, logins) on a fresh connection."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT disabled_at FROM identity_user WHERE id = :u"), {"u": user_id}
            )
        ).all()
        logins = (
            await conn.execute(
                text("SELECT count(*) FROM identity_login WHERE user_id = :u"), {"u": user_id}
            )
        ).scalar_one()
    return len(rows), (rows[0][0] if rows else None), int(logins)


# --------------------------------------------------------------------------- (a) disable and disable


async def test_ac30a_two_disables_both_succeed_and_exactly_one_changes_it(
    http: AsyncClient,
    make_account: object,
    engine: AsyncEngine,
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    target = await make_account()  # type: ignore[operator]
    url = f"{USERS}/{target.id}/disable"

    (a, b), waiting = await _race(
        engine,
        _USER_LOCK,
        {"t": target.id},
        http.post(url, headers=admin.headers),
        http.post(url, headers=admin.headers),
    )

    assert waiting == 2, "the overlap was not staged"
    assert (a.status_code, b.status_code) == (200, 200), (a.text, b.text)
    assert sorted([a.json()["changed"], b.json()["changed"]]) == [False, True]
    rows, disabled_at, logins = await _state(engine, target.id)
    assert (rows, logins) == (1, 0)
    assert disabled_at is not None
    assert {a.json()["disabled_at"], b.json()["disabled_at"]} == {a.json()["disabled_at"]}


# --------------------------------------------------------------------------- (b) disable and enable


async def test_ac30b_disable_and_enable_are_serialized_and_each_response_is_truthful(
    http: AsyncClient,
    make_account: object,
    engine: AsyncEngine,
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    target = await make_account()  # type: ignore[operator]

    (disable, enable), waiting = await _race(
        engine,
        _USER_LOCK,
        {"t": target.id},
        http.post(f"{USERS}/{target.id}/disable", headers=admin.headers),
        http.post(f"{USERS}/{target.id}/enable", headers=admin.headers),
    )

    assert waiting == 2, "the overlap was not staged"
    assert (disable.status_code, enable.status_code) == (200, 200), (disable.text, enable.text)
    _, disabled_at, logins = await _state(engine, target.id)
    assert logins == 0
    # Either order is legal; the responses must tell the truth about it.
    if disabled_at is None:  # the disable ran first, the enable undid it
        assert (disable.json()["changed"], enable.json()["changed"]) == (True, True)
    else:  # the enable found nothing to enable, the disable landed last
        assert (disable.json()["changed"], enable.json()["changed"]) == (True, False)


# --------------------------------------------------------------------------- (c) disable and erase


async def test_ac30c_disable_and_erase_end_with_no_row_and_a_later_disable_is_404(
    http: AsyncClient,
    make_account: object,
    engine: AsyncEngine,
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    target = await make_account()  # type: ignore[operator]

    (disable, erase), waiting = await _race(
        engine,
        _USER_LOCK,
        {"t": target.id},
        http.post(f"{USERS}/{target.id}/disable", headers=admin.headers),
        http.delete(f"{USERS}/{target.id}", headers=admin.headers),
    )

    assert waiting == 2, "the overlap was not staged"
    assert erase.status_code == 200, erase.text
    assert disable.status_code in (200, 404), disable.text
    if disable.status_code == 404:
        assert error_code(disable) == "user_not_found"
    assert (await _state(engine, target.id))[0] == 0
    after = await http.post(f"{USERS}/{target.id}/disable", headers=admin.headers)
    assert after.status_code == 404, after.text
    assert error_code(after) == "user_not_found"


# --------------------------------------------------------------------------- (d) erase and erase


async def test_ac30d_two_erasures_give_one_200_and_one_404(
    http: AsyncClient,
    make_account: object,
    engine: AsyncEngine,
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    target = await make_account()  # type: ignore[operator]

    (a, b), waiting = await _race(
        engine,
        _USER_LOCK,
        {"t": target.id},
        http.delete(f"{USERS}/{target.id}", headers=admin.headers),
        http.delete(f"{USERS}/{target.id}", headers=admin.headers),
    )

    assert waiting == 2, "the overlap was not staged"
    assert sorted([a.status_code, b.status_code]) == [200, 404], (a.text, b.text)
    loser = a if a.status_code == 404 else b
    assert error_code(loser) == "user_not_found"
    assert (await _state(engine, target.id))[0] == 0


# --------------------------------------------------------------------------- (e) disable and reset


async def test_ac30e_a_disable_racing_the_users_reset_confirm_ends_disabled_with_the_new_password(
    http: AsyncClient,
    make_account: object,
    world_app: FastAPI,
    engine: AsyncEngine,
    settings: Settings,
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    email = f"race-{uuid4().hex[:12]}@example.com"
    target = await make_account(email=email)  # type: ignore[operator]
    origin = {"Origin": settings.public_base_url}
    queue = install_recording_queue(world_app)
    requested = await http.post(RESET, json={"email": email}, headers=origin)
    assert requested.status_code == 202, requested.text
    mailer = RecordingAccountMailer()
    async with async_sessionmaker(engine, expire_on_commit=False)() as worker_session:
        await container._build_password_reset_delivery(settings, worker_session, mailer)(
            queue.resets[0]
        )
        await worker_session.commit()
    token = reset_token(mailer)

    (disable, confirm), waiting = await _race(
        engine,
        _USER_LOCK,
        {"t": target.id},
        http.post(f"{USERS}/{target.id}/disable", headers=admin.headers),
        http.post(RESET_CONFIRM, json={"token": token, "password": A_NEW_PASSWORD}, headers=origin),
    )

    assert waiting == 2, "the overlap was not staged"
    assert disable.status_code == 200, disable.text
    assert confirm.status_code == 204, confirm.text
    rows, disabled_at, logins = await _state(engine, target.id)
    assert (rows, logins) == (1, 0), "the account ends with no login"
    assert disabled_at is not None, "the account ends disabled"
    new = await http.post(LOGIN, json={"email": email, "password": A_NEW_PASSWORD}, headers=origin)
    assert new.status_code == 403, new.text
    assert error_code(new) == "account_disabled"
    old = await http.post(LOGIN, json={"email": email, "password": A_PASSWORD}, headers=origin)
    assert old.status_code == 401, "the new password is installed"


# --------------------------------------------------------------------------- (f) disable and refresh


async def test_ac30f_a_disable_racing_a_refresh_rotation_never_leaves_a_login(
    http: AsyncClient,
    make_account: object,
    engine: AsyncEngine,
    settings: Settings,
) -> None:
    admin = await make_account(admin=True)  # type: ignore[operator]
    target = await make_account()  # type: ignore[operator]
    origin = {"Origin": settings.public_base_url}
    assert (await _state(engine, target.id))[2] == 1, "premise: one login to race"

    (disable, refresh), waiting = await _race(
        engine,
        _LOGIN_LOCK,
        {"t": target.id},
        http.post(f"{USERS}/{target.id}/disable", headers=admin.headers),
        target.client.post(REFRESH, headers=origin),
    )

    assert waiting == 2, "the overlap was not staged"
    assert disable.status_code == 200, disable.text
    assert refresh.status_code in (200, 401, 409), refresh.text
    if refresh.status_code != 200:
        assert error_code(refresh) in ("not_signed_in", "refresh_in_progress")
    rows, disabled_at, logins = await _state(engine, target.id)
    assert (rows, logins) == (1, 0), "a login survived the disable"
    assert disabled_at is not None
    again = await target.client.post(REFRESH, headers=origin)
    assert again.status_code == 401, "the refresh after both is dead"
