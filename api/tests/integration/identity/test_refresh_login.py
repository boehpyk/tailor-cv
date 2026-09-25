"""Application tests for `RefreshLogin` (T13, RED).

Covers AC-10's five outcomes — rotated, raced, reused, expired, unknown — plus the concurrent-
rotation variant, against **in-memory repository fakes** (`tests/integration/fakes.py`); database
truths (the real `UPDATE … WHERE version = :v` race, AC-17) wait for T31. This file exercises what
`RefreshLogin.__call__` itself is responsible for, per its own skeleton docstring's numbered flow.

**Seeding note.** `Login.start` and `Login.rotate` (both real, GREEN since T9) record domain events
on the aggregate as a side effect. Every helper below calls `release_events()` immediately after
constructing or rotating a login *for seeding purposes only* — exactly what a login loaded from a
real repository would look like (it carries no buffered events from a past request) — so that a
test's `events.published` assertion reflects only what `RefreshLogin` itself published, not stale
history from the fixture.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from tailorcraft.application.identity.refresh_login import RefreshLogin
from tailorcraft.application.identity.results import Authenticated
from tailorcraft.domain.identity.errors import LoginNotFound, RefreshInProgress, RefreshTokenReused
from tailorcraft.domain.identity.events import RefreshTokenReuseDetected
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    LoginNotFoundReason,
    PasswordHash,
    TokenHash,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    CountingClock,
    FakeAccessTokenPort,
    FakeLoginRepository,
    FakeUserRepository,
    RecordingEventPublisher,
)

_LIFETIME = timedelta(days=30)
_T0 = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


def _token_hash(seed: str) -> TokenHash:
    return TokenHash(value=hashlib.sha256(seed.encode()).hexdigest())


def _assert_no_token_hash_leaked(exc: BaseException, *hashes: TokenHash) -> None:
    """Privacy: a raised `LoginNotFound`/`RefreshInProgress` never carries a token hash value in its
    message or its `args` — only a reason (closed enum) and a login **id** are allowed (module
    docstrings of `domain/identity/errors.py`)."""
    rendered = str(exc) + repr(exc.args)
    for token_hash in hashes:
        assert token_hash.value not in rendered


async def _seed_user(users: FakeUserRepository) -> User:
    user = User.register_with_password(
        users.next_identity(),
        EmailAddress.parse("alex@example.com"),
        PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$b2xkaGFzaA"),
        at=_T0,
    )
    user.release_events()
    await users.add(user)
    return user


async def _seed_login(
    logins: FakeLoginRepository,
    user: User,
    *,
    current_hash: TokenHash,
    at: datetime = _T0,
    lifetime: timedelta = _LIFETIME,
) -> Login:
    login = Login.start(logins.next_identity(), user.id, current_hash, at, lifetime)
    login.release_events()  # discard the seeding LoggedIn (see module docstring)
    await logins.add(login)
    return login


def _build_use_case(
    *,
    logins: FakeLoginRepository,
    users: FakeUserRepository,
    tokens: FakeAccessTokenPort,
    clock: CountingClock,
    events: RecordingEventPublisher,
) -> RefreshLogin:
    return RefreshLogin(logins, users, tokens, clock, events)


async def test_current_token_rotates_bumps_generation_and_issues_a_new_access_token(
    clock: FixedClock,
) -> None:
    """**rotated**: generation +1, the new current hash is `replacement`, the retired hash is
    recorded, an access token is issued for the login's user, and no event is published."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    logins = FakeLoginRepository()
    h1 = _token_hash("r1")
    login = await _seed_login(logins, user, current_hash=h1)
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    counting_clock = CountingClock(clock)
    use_case = _build_use_case(
        logins=logins, users=users, tokens=tokens, clock=counting_clock, events=events
    )

    h2 = _token_hash("r2")
    result = await use_case(h1, h2)

    assert isinstance(result, Authenticated)
    assert result.login is login
    assert login.current_token_hash == h2
    assert login.generation == 2
    retired = await logins.find_by_retired_token_hash(h1)
    assert retired is not None
    assert retired == (login, 1)
    assert tokens.issue_calls == [(user.id, clock.now())]
    assert events.published == []
    assert logins.removed == []
    assert counting_clock.calls == 1


async def test_retired_token_within_the_grace_is_raced_and_changes_nothing(
    clock: FixedClock,
) -> None:
    """**raced**: the immediate predecessor within `REFRESH_RACE_GRACE` — `RefreshInProgress`,
    login unchanged (generation, hash), nothing removed, nothing published."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    logins = FakeLoginRepository()
    h1 = _token_hash("r3")
    login = await _seed_login(logins, user, current_hash=h1)
    h2 = _token_hash("r4")
    retired = login.rotate(h2, _T0)
    login.release_events()  # rotate() records nothing, but keep the seeding pattern uniform
    await logins.save_rotation(login, retired)
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    raced_clock = FixedClock(_T0 + timedelta(seconds=5))
    counting_clock = CountingClock(raced_clock)
    use_case = _build_use_case(
        logins=logins, users=users, tokens=tokens, clock=counting_clock, events=events
    )

    h3 = _token_hash("r5")
    with pytest.raises(RefreshInProgress) as exc_info:
        await use_case(h1, h3)

    assert exc_info.value.login_id == login.id  # I-23's log line names the login
    _assert_no_token_hash_leaked(exc_info.value, h1, h2, h3)
    assert login.generation == 2
    assert login.current_token_hash == h2
    assert logins.removed == []
    assert events.published == []
    assert tokens.issue_calls == []


async def test_retired_token_outside_the_grace_is_reused_removes_the_login_and_publishes(
    clock: FixedClock,
) -> None:
    """**reused**: outside the 10 s grace — the login is removed, `RefreshTokenReuseDetected` is
    published with both generations, and `RefreshTokenReused` is raised."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    logins = FakeLoginRepository()
    h1 = _token_hash("r6")
    login = await _seed_login(logins, user, current_hash=h1)
    h2 = _token_hash("r7")
    retired = login.rotate(h2, _T0)
    login.release_events()
    await logins.save_rotation(login, retired)
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    reused_clock = FixedClock(_T0 + timedelta(seconds=11))
    counting_clock = CountingClock(reused_clock)
    use_case = _build_use_case(
        logins=logins, users=users, tokens=tokens, clock=counting_clock, events=events
    )

    h3 = _token_hash("r8")
    with pytest.raises(RefreshTokenReused):
        await use_case(h1, h3)

    assert logins.removed == [login.id]
    assert events.published == [
        RefreshTokenReuseDetected(
            user_id=user.id,
            login_id=login.id,
            generation_presented=1,
            generation_current=2,
            occurred_at=reused_clock.now(),
        )
    ]
    assert tokens.issue_calls == []


async def test_expired_login_is_deleted_on_sight_and_raises_login_not_found(
    clock: FixedClock,
) -> None:
    """**expired** (I-21), via the current-token path: `Login.rotate` refuses at `expires_at`
    (inclusive) with `LoginExpired`; the use case deletes the login and raises `LoginNotFound`."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    logins = FakeLoginRepository()
    h1 = _token_hash("r9")
    login = await _seed_login(logins, user, current_hash=h1, at=_T0, lifetime=timedelta(seconds=60))
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    expired_clock = FixedClock(_T0 + timedelta(seconds=60))  # inclusive boundary
    counting_clock = CountingClock(expired_clock)
    use_case = _build_use_case(
        logins=logins, users=users, tokens=tokens, clock=counting_clock, events=events
    )

    h2 = _token_hash("r10")
    with pytest.raises(LoginNotFound) as exc_info:
        await use_case(h1, h2)

    assert exc_info.value.reason is LoginNotFoundReason.EXPIRED  # I-21's log line
    assert exc_info.value.login_id == login.id  # the deleted login is known and logged
    _assert_no_token_hash_leaked(exc_info.value, h1, h2)
    assert logins.removed == [login.id]
    assert events.published == []
    assert tokens.issue_calls == []


async def test_expired_login_presented_via_a_retired_token_raises_login_not_found_with_reason_and_id(
    clock: FixedClock,
) -> None:
    """**expired** (I-21), via the *retired*-token path: `Login.judge_retired` checks `is_expired`
    before RACED/REUSED (`login.py`'s own docstring), so a retired token presented after the login's
    absolute lifetime answers the same deleted-on-sight `LoginNotFound` as the current-token path —
    `reason=EXPIRED` and the login's id, not a bare `LoginNotFound()`."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    logins = FakeLoginRepository()
    h1 = _token_hash("r15")
    login = await _seed_login(logins, user, current_hash=h1, at=_T0, lifetime=timedelta(seconds=60))
    h2 = _token_hash("r16")
    retired = login.rotate(h2, _T0)
    login.release_events()
    await logins.save_rotation(login, retired)
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    expired_clock = FixedClock(_T0 + timedelta(seconds=60))  # inclusive boundary
    counting_clock = CountingClock(expired_clock)
    use_case = _build_use_case(
        logins=logins, users=users, tokens=tokens, clock=counting_clock, events=events
    )

    h3 = _token_hash("r17")
    with pytest.raises(LoginNotFound) as exc_info:
        await use_case(h1, h3)  # h1 is now the *retired* hash, not current

    assert exc_info.value.reason is LoginNotFoundReason.EXPIRED
    assert exc_info.value.login_id == login.id
    _assert_no_token_hash_leaked(exc_info.value, h1, h2, h3)
    assert logins.removed == [login.id]
    assert events.published == []
    assert tokens.issue_calls == []


async def test_unknown_token_raises_login_not_found_and_changes_nothing(clock: FixedClock) -> None:
    """**unknown**: matches neither a current nor a retired hash — `LoginNotFound`, nothing removed,
    nothing published.

    Also asserts the *defaults* the skeleton already sets: `reason is UNKNOWN` and `login_id is None`
    (there is no login to name). Unlike the expired/raced/concurrent cases, this assertion passes
    today — `LoginNotFound()`'s bare defaults already are `UNKNOWN`/`None` — and is included for the
    same reason the failure contract lists I-20 at all: so a future change to the default is caught
    here rather than only in the router's translation."""
    users = FakeUserRepository()
    logins = FakeLoginRepository()
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    use_case = _build_use_case(
        logins=logins, users=users, tokens=tokens, clock=CountingClock(clock), events=events
    )

    presented = _token_hash("r11")
    replacement = _token_hash("r12")
    with pytest.raises(LoginNotFound) as exc_info:
        await use_case(presented, replacement)

    assert exc_info.value.reason is LoginNotFoundReason.UNKNOWN
    assert exc_info.value.login_id is None
    _assert_no_token_hash_leaked(exc_info.value, presented, replacement)
    assert logins.removed == []
    assert events.published == []
    assert tokens.issue_calls == []


async def test_a_concurrently_won_rotation_race_is_answered_refresh_in_progress_and_nothing_is_removed(
    clock: FixedClock,
) -> None:
    """The sixth outcome: the repository's own optimistic-concurrency refusal
    (`LoginConcurrentlyRotated`, simulated here by `FakeLoginRepository`'s
    `conflict_on_save_rotation`, mirroring `FakeTailoringRunRepository.conflict_on_save`) is
    translated to `RefreshInProgress`, never to a revocation. `Login.rotate` itself has already run
    by the time `save_rotation` is asked and rejects, so — unlike the **raced** outcome above, which
    is refused before any state changes — this scenario only asserts what AC-10 actually promises
    for it: `RefreshInProgress` raised and nothing removed."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    logins = FakeLoginRepository(conflict_on_save_rotation=1)
    h1 = _token_hash("r13")
    login = await _seed_login(logins, user, current_hash=h1)
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    use_case = _build_use_case(
        logins=logins, users=users, tokens=tokens, clock=CountingClock(clock), events=events
    )

    h2 = _token_hash("r14")
    with pytest.raises(RefreshInProgress) as exc_info:
        await use_case(h1, h2)

    assert exc_info.value.login_id == login.id  # I-25's log line names the login
    _assert_no_token_hash_leaked(exc_info.value, h1, h2)
    assert logins.removed == []
    assert tokens.issue_calls == []
