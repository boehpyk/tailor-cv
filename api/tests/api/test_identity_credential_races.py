"""Slice 2.5, T32 PROOF: the credential races, on real connections (AC-35, AC-36, AC-37, V-48 ... V-53).

Written from the spec's actor table (technical plan section 0.7, 0.8; ADR-0028 sections 5 and 6),
**never** from what the code happens to do. Every race here is **staged at the moment the spec names**
-- between the read and the write -- by a hook that lets the other request run to completion (or to
its lock) at that exact point, never before the request is sent, and every overlap is **proven**:
either the hook observed the other request's own statement waiting on a lock in `pg_stat_activity`
(`wait_for_lock_waiter`), or the other request had finished before the victim resumed.

**The staging seams are chosen so a mutation cannot remove them.** AC-35(a)/AC-36(a) hook the
*hasher's* `verify` (the ~50 ms the spec's race lives in), not the re-check the mutation deletes; a
hook on the deleted call would simply never fire and the test would go red for the wrong reason.
Where the seam *is* the code under test (AC-35(b)'s `FOR SHARE`), the test asserts the hook ran, so
the mutated run names what is missing.

Real, **committed** rows on `tailorcraft_test` (a refused request rolls back a shared session, so the
shared-session `app` fixture cannot tell a commit from an attempt), cleaned up in a `finally`. Each
connection is a real session per request (`concurrent_app`'s shape), `SET LOCAL lock_timeout` is
pinned on the request's own transaction through `_pin_lock_timeout` (it lives exactly as long as the
locks it guards, so it cannot be lost on a pooled connection), and a hanging test would be worse than
a failing one: every wait here is bounded and names what it was waiting for.

**Mutations observed red, and the source restored byte-exact (`git diff --exit-code`)** -- run 2026-10-02,
each applied to production source, one test file run, restored:

| Mutation (production source) | Observed red |
|---|---|
| `application/identity/log_in.py`: delete the `confirm_credential_unchanged` re-check | AC-35(a) `assert 200 == 401` (a live `Login` minted from the old password); AC-35(b) "never reached its credential re-check: not staged" |
| `infrastructure/persistence/repositories/identity/user.py`: drop `.with_for_update(read=True)` from `confirm_credential_unchanged` | AC-35(b) "the reset was not blocked by the login's lock: no overlap was proven" |
| `application/identity/delete_own_account.py`: delete the `locked.password_hash != verified_hash` compare | AC-36 `assert 204 == 403` (account erased); the reset-first erasure race `(204, '')`, `assert 204 == 403` |
| `application/identity/reset_password.py`: lock the reset row **before** the user (swap the two lines) | the no-deadlock test `assert 11 == 10` on `pg_stat_database.deadlocks` ("deadlock_detected was observed"); the V-53 test `assert 503 == 400`; the double reset-confirm test "never waited on the user lock" |
| `infrastructure/persistence/repositories/identity/pending_registration.py`: drop `.with_for_update()` from `lock_by_token_hash` | double confirm "the second confirm never waited on the row lock: no overlap was proven" |

Each restored byte-exact (`git diff --exit-code` empty): green, 10 passed.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.domain.identity.value_objects import Password, PasswordHash, PasswordVerdict
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.repositories.identity.login import (
    SqlAlchemyLoginRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.password_reset import (
    SqlAlchemyPasswordResetRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.pending_registration import (
    SqlAlchemyPendingRegistrationRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.account_mail_support import (
    confirmation_token,
    deliver_password_reset,
    deliver_registration,
    install_recording_queue,
    reset_token,
)
from tests.api.me_support import A_PASSWORD, build_concurrent_app, new_client, seed_user_and_sign_in
from tests.integration.claim_race_support import (
    LOCK_TIMEOUT_MS,
    assert_test_database,
    bound_cleanup_locks,
    wait_for_lock_waiter,
)
from tests.integration.fakes import RecordingAccountMailQueue

LOGIN_URL = "/api/auth/login"
REFRESH_URL = "/api/auth/refresh"
ME_URL = "/api/auth/me"
REGISTER_URL = "/api/auth/register"
CONFIRM_URL = "/api/auth/registration/confirm"
RESET_URL = "/api/auth/password-reset"
RESET_CONFIRM_URL = "/api/auth/password-reset/confirm"
DELETE_ACCOUNT_URL = "/api/auth/delete-account"

NEW_PASSWORD = "an entirely different passphrase 7"
OTHER_NEW_PASSWORD = "yet another unrelated passphrase 3"
WAIT_SECONDS = 5.0

_Real = Callable[..., Awaitable[Any]]


# ---------------------------------------------------------------------------------------------
# The world: one committed account, a real session per request, a hasher with a seam
# ---------------------------------------------------------------------------------------------


class _SeamHasher:
    """The app's real (cheap-parameter) hasher with two seams: a **one-shot** coroutine run right
    after the next `verify` returns (the moment the spec's race lives in), and a count of `hash`
    calls (a password is written once only if it was hashed once)."""

    def __init__(self, real: Argon2PasswordHasher) -> None:
        self._real = real
        self.after_verify: Callable[[], Awaitable[None]] | None = None
        self.hash_calls = 0

    async def hash(self, password: Password) -> PasswordHash:
        self.hash_calls += 1
        return await self._real.hash(password)

    async def verify(self, password: Password, against: PasswordHash | None) -> PasswordVerdict:
        verdict = await self._real.verify(password, against)
        hook, self.after_verify = self.after_verify, None  # one-shot, cleared before it runs
        if hook is not None:
            await hook()
        return verdict


@dataclass
class World:
    app: FastAPI
    settings: Settings
    engine: AsyncEngine
    queue: RecordingAccountMailQueue
    hasher: _SeamHasher
    email: str
    user_id: UUID
    bearer_token: str
    browser: AsyncClient  # holds the refresh cookie of the login the seed made
    extra_emails: list[str] = field(default_factory=list)

    @property
    def origin(self) -> dict[str, str]:
        return {"Origin": self.settings.public_base_url}

    @property
    def bearer(self) -> dict[str, str]:
        return {**self.origin, "Authorization": f"Bearer {self.bearer_token}"}


@pytest_asyncio.fixture
async def world(
    settings: Settings,
    engine: AsyncEngine,
    password_hasher: Argon2PasswordHasher,
    clear_redis: None,
) -> AsyncIterator[World]:
    """`clear_redis`: the request routes touch limiters, and Redis is not rolled back with Postgres."""
    assert_test_database(settings)
    app = build_concurrent_app(settings, engine, password_hasher)
    hasher = _SeamHasher(password_hasher)
    app.state.password_hasher = hasher
    queue = install_recording_queue(app)
    email = f"t32-{uuid4().hex[:12]}@example.com"
    browser = new_client(app)
    token, user_id = await seed_user_and_sign_in(browser, settings, email=email)
    built = World(app, settings, engine, queue, hasher, email, user_id, token, browser)
    try:
        yield built
    finally:
        await browser.aclose()
        async with engine.begin() as conn:
            await bound_cleanup_locks(conn)
            for address in (email, *built.extra_emails):
                await conn.execute(
                    text("DELETE FROM identity_pending_registration WHERE email = :e"),
                    {"e": address},
                )
                await conn.execute(
                    text("DELETE FROM identity_password_reset WHERE email = :e"), {"e": address}
                )
                await conn.execute(
                    text("DELETE FROM identity_user WHERE email = :e"), {"e": address}
                )


@pytest.fixture(autouse=True)
def _pin_lock_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """`SET LOCAL lock_timeout` on every request's own transaction, at its first user-row read.

    A request's session is a pooled connection and a bare `SET` does not survive a commit, so the
    timeout is set **inside** the transaction it must bound (`test_delete_account_lock_upgrade.py`'s
    reason): a regression that waits for ever fails in `LOCK_TIMEOUT_MS` with a message naming a
    lock, instead of hanging CI. Wrapped on `get_for_update`, `find_by_email` and `get`, the three
    reads every race here begins with; the real method then runs unchanged."""

    def wrap(real: _Real) -> _Real:
        async def pinned(repo: SqlAlchemyUserRepository, *args: Any, **kwargs: Any) -> Any:
            session: Any = repo._session
            await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'"))
            return await real(repo, *args, **kwargs)

        return pinned

    for name in ("get_for_update", "find_by_email", "get"):
        monkeypatch.setattr(
            SqlAlchemyUserRepository, name, wrap(getattr(SqlAlchemyUserRepository, name))
        )


# ---------------------------------------------------------------------------------------------
# Requests (each on its own client: its own cookie jar, its own connection) and reads
# ---------------------------------------------------------------------------------------------


async def _post(world: World, url: str, headers: dict[str, str], **json: object) -> Response:
    async with new_client(world.app) as client:
        return await client.post(url, json=json, headers=headers)


async def _login(world: World, password: str) -> Response:
    return await _post(world, LOGIN_URL, world.origin, email=world.email, password=password)


async def _reset_confirm(world: World, token: str, password: str = NEW_PASSWORD) -> Response:
    return await _post(world, RESET_CONFIRM_URL, world.origin, token=token, password=password)


async def _delete_account(world: World, password: str = A_PASSWORD) -> Response:
    return await _post(world, DELETE_ACCOUNT_URL, world.bearer, password=password)


async def _mint_reset_token(world: World) -> str:
    """The reset link's token, the way a person gets it: request, then the worker's own delivery."""
    response = await _post(world, RESET_URL, world.origin, email=world.email)
    assert response.status_code == 202, response.text
    async with world.app.state.session_factory() as session:
        mailer = await deliver_password_reset(world.settings, session, world.queue.resets[-1])
    return reset_token(mailer)


async def _scalar(world: World, sql: str, **params: object) -> Any:
    async with world.engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def _login_count(world: World) -> int:
    return int(
        await _scalar(
            world, "SELECT count(*) FROM identity_login WHERE user_id = :u", u=world.user_id
        )
    )


async def _user_exists(world: World) -> bool:
    return bool(
        await _scalar(world, "SELECT count(*) FROM identity_user WHERE id = :u", u=world.user_id)
    )


async def _password_hash(world: World) -> str:
    return str(
        await _scalar(
            world, "SELECT password_hash FROM identity_user WHERE id = :u", u=world.user_id
        )
    )


async def _deadlock_count(world: World) -> int:
    return int(
        await _scalar(
            world, "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()"
        )
    )


def _code(response: Response) -> str:
    body = response.json()
    assert "error" in body, f"expected the error envelope, got {body!r}"
    return str(body["error"]["code"])


async def _blocked_on_a_lock(world: World, task: asyncio.Task[Response], *fragments: str) -> bool:
    """True if `task`'s own statement is observed **waiting on a lock** (`pg_stat_activity`) before
    the task finishes; False if the task finished first (it never waited). Bounded."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_SECONDS
    while loop.time() < deadline and not task.done():
        with contextlib.suppress(AssertionError):
            await wait_for_lock_waiter(world.engine, *fragments, within=0.2)
            return True
    return False


# ---------------------------------------------------------------------------------------------
# AC-35 -- login vs. reset, both orders
# ---------------------------------------------------------------------------------------------


async def test_a_login_whose_password_was_reset_during_its_verify_is_401_and_leaves_no_login(
    world: World,
) -> None:
    """AC-35(a): the login read the user and verified the **old** password; a reset commits while it
    resumes; its re-check then finds the hash replaced. 401 `invalid_credentials`, and **no** `Login`
    of this account survives (the reset deleted the seed's and the login never wrote its own).

    Staged on the hasher's `verify` (the 50 ms the race lives in): the hook runs the complete reset
    after `verify` returned a match and before the login's next statement.

    **Mutation (delete the re-check from `LogIn`) -> red:** the login answers 200 and a live `Login`
    minted from the old password remains -- `assert 200 == 401`. Restored: green."""
    token = await _mint_reset_token(world)
    reset_statuses: list[int] = []

    async def reset_now() -> None:
        reset_statuses.append((await _reset_confirm(world, token)).status_code)

    world.hasher.after_verify = reset_now
    old_hash = await _password_hash(world)

    response = await _login(world, A_PASSWORD)

    assert reset_statuses == [204], "the reset must have committed inside the login's verify"
    assert await _password_hash(world) != old_hash
    assert response.status_code == 401, response.text
    assert _code(response) == "invalid_credentials"
    assert "tc_refresh" not in response.headers.get("set-cookie", "")
    assert await _login_count(world) == 0, "a Login minted from the old password survived the reset"


async def test_a_reset_waits_for_a_login_holding_its_shared_lock_and_then_deletes_that_login(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-35(b): the login's re-check succeeded and holds its `FOR SHARE`; the reset starts **now**.
    The reset must wait (observed: its `FOR UPDATE` on `identity_user` is a lock waiter), the login
    commits its `Login`, and the reset then deletes it.

    **Mutation (drop `.with_for_update(read=True)` from `confirm_credential_unchanged`) -> red:** the
    reset is never blocked (it commits inside the login's window) and the login's `Login` survives it.
    Restored: green. (Deleting the whole re-check is red here too: the hook is never reached.)"""
    token = await _mint_reset_token(world)
    real = SqlAlchemyUserRepository.confirm_credential_unchanged
    state: dict[str, Any] = {"reached": False, "blocked": False, "task": None}

    async def staged(repo: SqlAlchemyUserRepository, *args: Any, **kwargs: Any) -> bool:
        result: bool = await real(repo, *args, **kwargs)
        state["reached"] = True
        task = asyncio.create_task(_reset_confirm(world, token))
        state["task"] = task
        state["blocked"] = await _blocked_on_a_lock(world, task, "identity_user", "for update")
        return result

    monkeypatch.setattr(SqlAlchemyUserRepository, "confirm_credential_unchanged", staged)

    login = await _login(world, A_PASSWORD)

    assert state["reached"], "the login never reached its credential re-check: not staged"
    reset = await state["task"]
    assert state["blocked"], "the reset was not blocked by the login's lock: no overlap was proven"
    assert login.status_code == 200, login.text
    assert reset.status_code == 204, reset.text
    assert await _login_count(world) == 0, "the login's Login survived the reset that came after it"


# ---------------------------------------------------------------------------------------------
# AC-36 / V-51 / V-53 -- delete-account vs. reset
# ---------------------------------------------------------------------------------------------


async def test_a_deletion_verified_with_the_old_password_after_a_reset_is_403_and_erases_nothing(
    world: World,
) -> None:
    """AC-36: the deletion verified the **old** password; a reset commits before its erasure lock.
    The password it checked is no longer the account's: 403 `password_incorrect`, the account intact.

    **Mutation (delete the `locked.password_hash != verified_hash` compare from `DeleteOwnAccount`)
    -> red:** the account is erased, `assert 204 == 403`. Restored: green."""
    token = await _mint_reset_token(world)
    reset_statuses: list[int] = []

    async def reset_now() -> None:
        reset_statuses.append((await _reset_confirm(world, token)).status_code)

    world.hasher.after_verify = reset_now

    response = await _delete_account(world)

    assert reset_statuses == [204], "the reset must have committed inside the deletion's verify"
    assert response.status_code == 403, response.text
    assert _code(response) == "password_incorrect"
    assert await _user_exists(world), "an account was erased on the strength of a replaced password"


async def test_a_reset_arriving_while_an_erasure_holds_the_user_lock_answers_link_invalid(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V-53 / AC-37, erasure first: the deletion holds the user row `FOR UPDATE`; the reset (which
    already read its link) starts now and **waits** (observed); the erasure commits and takes the
    account and its resets; the reset's lock then finds no user. To the holder of the link that is a
    **400 `link_invalid`**, not a 401 or a 500 (T29's `UserNotFound` translation).

    Staged at the deletion's *first* `get_for_update` (its re-check lock); later calls (the reset's
    own, erasure's re-entry) pass through."""
    token = await _mint_reset_token(world)
    real = SqlAlchemyUserRepository.get_for_update
    state: dict[str, Any] = {"armed": True, "blocked": False, "task": None}

    async def staged(repo: SqlAlchemyUserRepository, *args: Any, **kwargs: Any) -> Any:
        found = await real(repo, *args, **kwargs)
        if state["armed"]:
            state["armed"] = False  # synchronous: the reset's own call passes straight through
            task = asyncio.create_task(_reset_confirm(world, token))
            state["task"] = task
            state["blocked"] = await _blocked_on_a_lock(world, task, "identity_user", "for update")
        return found

    monkeypatch.setattr(SqlAlchemyUserRepository, "get_for_update", staged)

    deletion = await _delete_account(world)

    assert state["task"] is not None, "the deletion never reached its lock: not staged"
    reset = await state["task"]
    assert state["blocked"], (
        "the reset was not blocked by the erasure's lock: no overlap was proven"
    )
    assert deletion.status_code == 204, deletion.text
    assert reset.status_code == 400, reset.text
    assert _code(reset) == "link_invalid"
    assert not await _user_exists(world)
    assert (
        await _scalar(
            world,
            "SELECT count(*) FROM identity_password_reset WHERE email = :e",
            e=world.email,
        )
        == 0
    )


async def test_erasure_and_reset_confirm_queue_on_the_user_row_and_never_deadlock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-37, reset first: the reset holds its locks (the hook runs right after it locked the reset
    row); a deletion verified with the old password starts **now** and queues behind it (a lock
    waiter on `identity_user`); the reset commits; the deletion's re-check then sees the new hash.
    No deadlock: `pg_stat_database.deadlocks` does not move, and the answers are 204 and 403.

    **Mutation (lock the reset row *before* the user in `ResetPassword`) -> red:** the erasure takes the
    user row and its cascade waits for the reset row the reset holds, while the reset waits for the
    user row: PostgreSQL aborts one (`deadlock_detected`), `pg_stat_database.deadlocks` rises by 1 and
    one request answers 503. Restored: green. The hook is order-independent (it fires after the reset
    row is locked, whichever lock came first), so the same staging proves both orders."""
    token = await _mint_reset_token(world)
    real = SqlAlchemyPasswordResetRepository.lock_by_token_hash
    state: dict[str, Any] = {"task": None, "waited": False}
    deadlocks_before = await _deadlock_count(world)

    async def staged(repo: SqlAlchemyPasswordResetRepository, *args: Any, **kwargs: Any) -> Any:
        found = await real(repo, *args, **kwargs)
        if state["task"] is None:
            task = asyncio.create_task(_delete_account(world))
            state["task"] = task
            state["waited"] = await _blocked_on_a_lock(world, task, "identity_user")
        return found

    monkeypatch.setattr(SqlAlchemyPasswordResetRepository, "lock_by_token_hash", staged)

    reset = await _reset_confirm(world, token)
    assert state["task"] is not None, "the reset never locked its row: not staged"
    deletion = await state["task"]

    assert state["waited"], "the erasure never queued on a lock: no overlap was proven"
    assert await _deadlock_count(world) == deadlocks_before, "deadlock_detected was observed"
    assert reset.status_code == 204, (reset.status_code, reset.text)
    assert deletion.status_code == 403, (deletion.status_code, deletion.text)
    assert _code(deletion) == "password_incorrect"
    assert await _user_exists(world)


# ---------------------------------------------------------------------------------------------
# AC-37 -- double submit
# ---------------------------------------------------------------------------------------------


async def test_two_concurrent_confirms_of_one_link_are_one_204_one_400_and_one_user(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-37 / V-36 / V-49: two confirms of one registration link. The second starts while the first
    holds the pending row's `FOR UPDATE` (observed as a lock waiter on `identity_pending_registration`),
    waits, and then finds the row gone: exactly one 204, one 400 `link_invalid`, exactly one `User`.

    **Mutation (drop `.with_for_update()` from the pending repository's `lock_by_token_hash`) -> red:**
    the second confirm never waits (it runs through the first's window), so the overlap assertion
    fails (and, left to race, the loser's insert would answer 409, not 400). Restored: green."""
    address = f"t32-confirm-{uuid4().hex[:12]}@example.com"
    world.extra_emails.append(address)
    response = await _post(world, REGISTER_URL, world.origin, email=address, password=A_PASSWORD)
    assert response.status_code == 202, response.text
    async with world.app.state.session_factory() as session:
        mailer = await deliver_registration(world.settings, session, world.queue.registrations[-1])
    token = confirmation_token(mailer)

    real = SqlAlchemyPendingRegistrationRepository.lock_by_token_hash
    state: dict[str, Any] = {"task": None, "blocked": False}

    async def staged(
        repo: SqlAlchemyPendingRegistrationRepository, *args: Any, **kwargs: Any
    ) -> Any:
        found = await real(repo, *args, **kwargs)
        if state["task"] is None:
            task = asyncio.create_task(_post(world, CONFIRM_URL, world.origin, token=token))
            state["task"] = task
            state["blocked"] = await _blocked_on_a_lock(
                world, task, "identity_pending_registration", "for update"
            )
        return found

    monkeypatch.setattr(SqlAlchemyPendingRegistrationRepository, "lock_by_token_hash", staged)

    first = await _post(world, CONFIRM_URL, world.origin, token=token)
    assert state["task"] is not None, "the first confirm never locked the row: not staged"
    second = await state["task"]

    assert state["blocked"], (
        "the second confirm never waited on the row lock: no overlap was proven"
    )
    assert first.status_code == 204, first.text
    assert second.status_code == 400, second.text
    assert _code(second) == "link_invalid"
    assert (
        await _scalar(world, "SELECT count(*) FROM identity_user WHERE email = :e", e=address) == 1
    )


async def test_two_concurrent_reset_confirms_of_one_link_are_one_204_one_400_and_one_hash(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-37 / V-49: two confirms of one reset link, with **different** passwords. The second starts
    while the first holds the user row (observed), waits, and then finds the link gone: one 204, one
    400 `link_invalid`. The hash was **written once**: the hasher was called once, the winner's
    password signs in and the loser's does not."""
    token = await _mint_reset_token(world)
    real = SqlAlchemyPasswordResetRepository.lock_by_token_hash
    state: dict[str, Any] = {"task": None, "blocked": False}

    async def staged(repo: SqlAlchemyPasswordResetRepository, *args: Any, **kwargs: Any) -> Any:
        found = await real(repo, *args, **kwargs)
        if state["task"] is None:
            task = asyncio.create_task(_reset_confirm(world, token, OTHER_NEW_PASSWORD))
            state["task"] = task
            state["blocked"] = await _blocked_on_a_lock(world, task, "identity_user", "for update")
        return found

    monkeypatch.setattr(SqlAlchemyPasswordResetRepository, "lock_by_token_hash", staged)
    hashes_before = world.hasher.hash_calls

    first = await _reset_confirm(world, token, NEW_PASSWORD)
    assert state["task"] is not None, "the first reset never locked its row: not staged"
    second = await state["task"]

    assert state["blocked"], "the second reset never waited on the user lock: no overlap was proven"
    assert first.status_code == 204, first.text
    assert second.status_code == 400, second.text
    assert _code(second) == "link_invalid"
    assert world.hasher.hash_calls - hashes_before == 1, "the password was hashed more than once"
    assert (await _login(world, NEW_PASSWORD)).status_code == 200
    assert (await _login(world, OTHER_NEW_PASSWORD)).status_code == 401


# ---------------------------------------------------------------------------------------------
# AC-37 -- reset vs. refresh
# ---------------------------------------------------------------------------------------------


async def test_a_refresh_after_a_committed_reset_is_401_not_signed_in(world: World) -> None:
    """AC-37 / V-52: after the reset commits, the old refresh cookie (which worked) is 401."""
    assert (await world.browser.post(REFRESH_URL, headers=world.origin)).status_code == 200
    token = await _mint_reset_token(world)
    assert (await _reset_confirm(world, token)).status_code == 204

    response = await world.browser.post(REFRESH_URL, headers=world.origin)

    assert response.status_code == 401, response.text
    assert _code(response) == "not_signed_in"


async def test_a_reset_arriving_during_a_rotation_deletes_the_rotated_login_and_the_access_token_lives_on(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-37 / V-52, rotation first: the refresh has written its rotation (the login row is locked
    by its `UPDATE`); the reset starts now and its `DELETE` of the account's logins **waits**
    (observed). The refresh commits and answers 200 with a new cookie and a fresh access token; the
    reset then deletes the rotated login. **No login survives** -- the new cookie is 401.

    **OQ-16, stated and asserted:** the access token the refresh minted just before the commit stays
    valid for its <= 15 minutes (`/api/auth/me` answers 200). The reset revokes logins, not tokens."""
    token = await _mint_reset_token(world)
    real = SqlAlchemyLoginRepository.save_rotation
    state: dict[str, Any] = {"task": None, "blocked": False}

    async def staged(repo: SqlAlchemyLoginRepository, *args: Any, **kwargs: Any) -> None:
        await real(repo, *args, **kwargs)
        task = asyncio.create_task(_reset_confirm(world, token))
        state["task"] = task
        state["blocked"] = await _blocked_on_a_lock(world, task, "identity_login", "delete")

    monkeypatch.setattr(SqlAlchemyLoginRepository, "save_rotation", staged)

    refreshed = await world.browser.post(REFRESH_URL, headers=world.origin)
    assert state["task"] is not None, "the refresh never rotated: not staged"
    reset = await state["task"]

    assert state["blocked"], "the reset's login DELETE never waited on the rotation: no overlap"
    assert refreshed.status_code == 200, refreshed.text
    assert reset.status_code == 204, reset.text
    assert await _login_count(world) == 0, "a rotated login survived the reset"
    new_access = str(refreshed.json()["access_token"])
    assert (
        await _post_get(world, ME_URL, {"Authorization": f"Bearer {new_access}"})
    ).status_code == 200, "OQ-16: the token minted just before the commit lives out its lifetime"
    again = await world.browser.post(REFRESH_URL, headers=world.origin)
    assert again.status_code == 401, again.text


async def test_a_refresh_whose_login_a_reset_deleted_between_its_read_and_its_write_survives_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-37 / V-52, reset first: the reset commits **between the refresh's read and its write** (the
    hook runs the whole reset before `save_rotation`). The rotation then updates a row that is gone.
    Whatever the answer, **no login survives** and **no new refresh cookie is issued**; and a retry
    with the old cookie is 401 `not_signed_in`."""
    token = await _mint_reset_token(world)
    real = SqlAlchemyLoginRepository.save_rotation
    state: dict[str, Any] = {"reset": None}

    async def staged(repo: SqlAlchemyLoginRepository, *args: Any, **kwargs: Any) -> None:
        state["reset"] = (await _reset_confirm(world, token)).status_code
        await real(repo, *args, **kwargs)

    monkeypatch.setattr(SqlAlchemyLoginRepository, "save_rotation", staged)

    refreshed = await world.browser.post(REFRESH_URL, headers=world.origin)

    assert state["reset"] == 204, "the reset must have committed between the read and the write"
    assert refreshed.status_code != 200, refreshed.text
    assert "tc_refresh" not in refreshed.headers.get("set-cookie", "") or (
        "Max-Age=0" in refreshed.headers.get("set-cookie", "")
    ), "a new refresh cookie was issued for a login the reset deleted"
    assert await _login_count(world) == 0, "a login survived the reset"
    monkeypatch.undo()
    retry = await world.browser.post(REFRESH_URL, headers=world.origin)
    assert retry.status_code == 401, retry.text
    assert _code(retry) == "not_signed_in"


async def _post_get(world: World, url: str, headers: dict[str, str]) -> Response:
    async with new_client(world.app) as client:
        return await client.get(url, headers=headers)
