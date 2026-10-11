"""Application tests for `LogIn` refusing a disabled account (slice 4.2, T11 RED, AC-9 (a)-(d)).

(a) and (b) run on the `session` fixture with real repositories. (c) and (d) are races, staged on
**two real connections** (committed seeds, one engine connection per actor), with the overlap proven
from `pg_stat_activity` rather than assumed:

- (c) `DisableAccount` commits on another connection *after* `LogIn`'s verify and *before* its locked
  re-check (the hook wraps `confirm_credential_unchanged`, the method the re-check calls), while the
  login's own backend is observed mid-transaction. The re-check's `FOR SHARE` then reads the
  committed row, so the login must be refused.
- (d) `LogIn` holds the re-check's `FOR SHARE` (the hook runs *after* the re-check returns) when
  `DisableAccount` starts; the disable's backend is observed **waiting on a lock** before the login
  is let go. After both commit the user must hold no login.

Every wait has a timeout: a race test that hangs is worse than one that fails. Against a `LogIn` with
no step 4b these go red on `DID NOT RAISE AccountDisabled` ((c)) and on a surviving login ((d)).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.application.identity.disable_account import DisableAccount
from tailorcraft.application.identity.log_in import LogIn
from tailorcraft.domain.identity.errors import AccountDisabled, InvalidCredentials
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    PasswordHash,
    PasswordVerdict,
    UserId,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    CountingClock,
    FakeAccessTokenPort,
    RecordingEventPublisher,
    RecordingFailedLoginObserver,
    RecordingPasswordHasher,
)
from tests.integration.identity._account_support import (
    PHC,
    SpyLogins,
    SpyUsers,
    login_count,
    seed_user,
    token_hash,
)

_LIFETIME = timedelta(days=30)
_WAIT = 10.0


def _email(user_id_hint: str) -> str:
    return f"{user_id_hint}@example.com"


class _Rig:
    def __init__(
        self,
        session: AsyncSession,
        clock: FixedClock,
        verdict: PasswordVerdict,
        users: SpyUsers | None = None,
    ) -> None:
        self.log: list[str] = []
        self.users = users or SpyUsers(session, self.log)
        self.logins = SpyLogins(session, self.log)
        self.hasher = RecordingPasswordHasher(verify_result=verdict, hash_result=PHC)
        self.tokens = FakeAccessTokenPort()
        self.events = RecordingEventPublisher()
        self.failed = RecordingFailedLoginObserver()
        self.use_case = LogIn(
            self.users,
            self.logins,
            self.hasher,
            self.tokens,
            CountingClock(clock),
            self.events,
            self.failed,
            _LIFETIME,
        )


# --- (a) and (b): one connection ------------------------------------------------------------


@pytest.mark.parametrize("verdict", [PasswordVerdict.MATCH, PasswordVerdict.MATCH_NEEDS_REHASH])
async def test_a_correct_password_for_a_disabled_account_raises_account_disabled(
    session: AsyncSession, clock: FixedClock, verdict: PasswordVerdict
) -> None:
    address = _email(uuid4().hex[:12])
    user_id = await seed_user(session, clock, email=address, disabled=True)
    rig = _Rig(session, clock, verdict)

    with pytest.raises(AccountDisabled) as raised:
        await rig.use_case(address, "the correct password", token_hash("r"))

    assert raised.value.user_id == user_id
    assert await login_count(session, user_id) == 0
    assert "logins.add" not in rig.log
    assert "users.save" not in rig.log, "no rehash is saved for a refused login"
    assert rig.tokens.issue_calls == []
    assert rig.events.published == []
    assert rig.failed.wrong_password_calls == []
    assert rig.failed.unknown_email_calls == 0


async def test_a_wrong_password_for_a_disabled_account_is_invalid_credentials_as_for_an_enabled_one(
    session: AsyncSession, clock: FixedClock
) -> None:
    address = _email(uuid4().hex[:12])
    user_id = await seed_user(session, clock, email=address, disabled=True)
    rig = _Rig(session, clock, PasswordVerdict.MISMATCH)

    with pytest.raises(InvalidCredentials) as raised:
        await rig.use_case(address, "a wrong password", token_hash("r"))

    assert type(raised.value) is InvalidCredentials, "a wrong password must not reveal the state"
    assert rig.failed.wrong_password_calls == [user_id]
    assert await login_count(session, user_id) == 0


async def test_a_correct_password_for_an_enabled_account_still_logs_in(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The control: step 4b must not refuse everyone."""
    address = _email(uuid4().hex[:12])
    user_id = await seed_user(session, clock, email=address)
    rig = _Rig(session, clock, PasswordVerdict.MATCH)

    outcome = await rig.use_case(address, "the correct password", token_hash("r"))

    assert outcome.user.id == user_id
    assert await login_count(session, user_id) == 1


# --- (c) and (d): two real connections ------------------------------------------------------


@pytest_asyncio.fixture
async def actors(engine: AsyncEngine) -> AsyncIterator[Callable[[], AsyncSession]]:
    """Fresh committing sessions on the test engine, closed afterwards; seeds are removed by id."""
    opened: list[AsyncSession] = []
    maker = async_sessionmaker(engine, expire_on_commit=False)

    def make() -> AsyncSession:
        s = maker()
        opened.append(s)
        return s

    make.opened = opened  # type: ignore[attr-defined]
    yield make
    for s in opened:
        await s.close()


async def _committed_user(engine: AsyncEngine, address: str, clock: FixedClock) -> UserId:
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        user = User.register_with_password(
            SpyUsers(s, []).next_identity(), EmailAddress.parse(address), PHC, clock.now()
        )
        user.release_events()
        await SpyUsers(s, []).add(user)
        await s.commit()
        return user.id


async def _delete(engine: AsyncEngine, user_id: UserId, actors: Callable[[], AsyncSession]) -> None:
    """Release every actor's locks first: a failed assertion must not leave a `FOR SHARE` that makes
    this cleanup wait for ever."""
    for s in actors.opened:  # type: ignore[attr-defined]
        await s.rollback()
    async with engine.connect() as conn:
        await conn.execute(text("SET lock_timeout = '5s'"))
        await conn.execute(text("DELETE FROM identity_user WHERE id = :u"), {"u": user_id.value})
        await conn.commit()


async def _backend_pid(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def _activity(engine: AsyncEngine, pid: int) -> tuple[str, str | None]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT state, wait_event_type FROM pg_stat_activity WHERE pid = :p"),
                {"p": pid},
            )
        ).one()
    return row.state, row.wait_event_type


async def _commit_a_disable(
    maker_session: Callable[[], AsyncSession], clock: FixedClock, target: UserId
) -> None:
    s = maker_session()
    events = RecordingEventPublisher()
    log: list[str] = []
    await DisableAccount(SpyUsers(s, log), SpyLogins(s, log), clock, events)(target)
    await s.commit()


class _HookedUsers(SpyUsers):
    """Runs `before_confirm` ahead of the re-check and `after_confirm` once the re-check has
    returned (the `FOR SHARE` is then held)."""

    def __init__(
        self,
        session: AsyncSession,
        before_confirm: Callable[[], Awaitable[None]] | None = None,
        after_confirm: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(session, [])
        self._before = before_confirm
        self._after = after_confirm

    async def confirm_credential_unchanged(self, user_id: UserId, seen: PasswordHash) -> bool:
        if self._before is not None:
            await self._before()
        result = await super().confirm_credential_unchanged(user_id, seen)
        if self._after is not None:
            await self._after()
        return result


async def test_a_disable_committing_between_the_verify_and_the_recheck_refuses_the_login(
    engine: AsyncEngine, clock: FixedClock, actors: Callable[[], AsyncSession]
) -> None:
    address = _email(uuid4().hex[:12])
    target = await _committed_user(engine, address, clock)
    try:
        login_session = actors()
        pid = await _backend_pid(login_session)
        observed: list[tuple[str, str | None]] = []

        async def disable_elsewhere() -> None:
            # Overlap proof: the login's backend is inside its open transaction right now.
            observed.append(await _activity(engine, pid))
            await asyncio.wait_for(_commit_a_disable(actors, clock, target), _WAIT)

        users = _HookedUsers(login_session, before_confirm=disable_elsewhere)
        rig = _Rig(login_session, clock, PasswordVerdict.MATCH, users)

        with pytest.raises(AccountDisabled):
            await asyncio.wait_for(
                rig.use_case(address, "the correct password", token_hash("r")), _WAIT * 2
            )
        await login_session.rollback()

        assert observed == [("idle in transaction", None)], "the two requests did not overlap"
        async with engine.connect() as conn:
            logins = (
                await conn.execute(
                    text("SELECT count(*) FROM identity_login WHERE user_id = :u"),
                    {"u": target.value},
                )
            ).scalar_one()
            stored = (
                await conn.execute(
                    text("SELECT disabled_at FROM identity_user WHERE id = :u"),
                    {"u": target.value},
                )
            ).scalar_one()
        assert logins == 0
        assert stored is not None
        assert rig.tokens.issue_calls == []
    finally:
        await _delete(engine, target, actors)


async def test_a_disable_starting_while_the_login_holds_its_share_lock_waits_and_then_revokes_it(
    engine: AsyncEngine, clock: FixedClock, actors: Callable[[], AsyncSession]
) -> None:
    address = _email(uuid4().hex[:12])
    target = await _committed_user(engine, address, clock)
    try:
        login_session = actors()
        disable_session = actors()
        disable_pid = await _backend_pid(disable_session)
        disable_task: asyncio.Task[None] | None = None
        observed: list[tuple[str, str | None]] = []

        async def start_the_disable_and_wait_for_it_to_block() -> None:
            nonlocal disable_task

            async def run() -> None:
                events = RecordingEventPublisher()
                log: list[str] = []
                await DisableAccount(
                    SpyUsers(disable_session, log), SpyLogins(disable_session, log), clock, events
                )(target)
                await disable_session.commit()

            disable_task = asyncio.create_task(run())
            deadline = asyncio.get_running_loop().time() + _WAIT
            while True:
                if disable_task.done():
                    # Surface why it ended early (e.g. a skeleton), never wait out the timeout.
                    disable_task.result()
                    raise AssertionError("the disable finished without waiting for the login")
                state = await _activity(engine, disable_pid)
                if state[1] == "Lock":
                    observed.append(state)
                    return
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError(f"the disable never blocked on the login: {state}")
                await asyncio.sleep(0.02)

        users = _HookedUsers(
            login_session, after_confirm=start_the_disable_and_wait_for_it_to_block
        )
        rig = _Rig(login_session, clock, PasswordVerdict.MATCH, users)

        # The login is allowed to finish: its re-check ran before the disable could commit.
        try:
            await asyncio.wait_for(
                rig.use_case(address, "the correct password", token_hash("r")), _WAIT * 2
            )
        except AccountDisabled:
            pytest.fail("the login read the account as disabled while the disable was blocked")
        await login_session.commit()

        assert disable_task is not None
        await asyncio.wait_for(disable_task, _WAIT)

        assert observed == [("active", "Lock")]
        async with engine.connect() as conn:
            logins = (
                await conn.execute(
                    text("SELECT count(*) FROM identity_login WHERE user_id = :u"),
                    {"u": target.value},
                )
            ).scalar_one()
            stored = (
                await conn.execute(
                    text("SELECT disabled_at FROM identity_user WHERE id = :u"),
                    {"u": target.value},
                )
            ).scalar_one()
        assert stored is not None, "the disable did not land"
        assert logins == 0, "the login that committed under the disable's wait survived it"
    finally:
        await _delete(engine, target, actors)
