"""Application tests for `LogIn` (T13, RED).

Covers AC-9 and the login half of the failure contract (I-9 … I-12) against **in-memory repository
fakes**, a **recording hasher** and a **recording `FailedLoginObserver`**
(`tests/integration/fakes.py`) — database truths wait for T31. This file exercises what
`LogIn.__call__` itself is responsible for, per its own skeleton docstring's numbered flow.

**Why this file leans hard on `verify_calls`, not just counts.** AC-9's central claim is that an
unknown email and a wrong password cost the *same* — one `hasher.verify(password, None-or-hash)`
call each — and raise the *same* `InvalidCredentials`, with nothing downstream able to tell them
apart by inspecting the error. `RecordingPasswordHasher.verify_calls` is the only place that
distinction is provable, since the exception itself carries nothing (AC-9) and the response is
required to be byte-identical (AC-28).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from tailorcraft.application.identity.log_in import LogIn
from tailorcraft.application.identity.results import Authenticated
from tailorcraft.domain.identity.errors import InvalidCredentials, InvalidEmailAddress
from tailorcraft.domain.identity.events import LoggedIn, UserPasswordRehashed
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    InvalidEmailReason,
    PasswordHash,
    PasswordVerdict,
    TokenHash,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    CountingClock,
    FakeAccessTokenPort,
    FakeLoginRepository,
    FakeUserRepository,
    RecordingEventPublisher,
    RecordingFailedLoginObserver,
    RecordingPasswordHasher,
)

_REFRESH_LIFETIME = timedelta(days=30)
_OLD_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$b2xkaGFzaA")


def _token_hash(seed: str) -> TokenHash:
    return TokenHash(value=hashlib.sha256(seed.encode()).hexdigest())


async def _seed_user(
    users: FakeUserRepository,
    *,
    email: str = "alex@example.com",
    password_hash: PasswordHash = _OLD_HASH,
) -> User:
    """A user "already in the database" — constructed directly, bypassing `RegisterUser`, with its
    creation event discarded: a user loaded by a real repository carries no buffered events, and
    this file is not testing registration."""
    user = User.register_with_password(
        users.next_identity(),
        EmailAddress.parse(email),
        password_hash,
        at=datetime(2026, 9, 4, 0, 0, 0, tzinfo=UTC),
    )
    user.release_events()
    await users.add(user)
    return user


def _build_use_case(
    *,
    users: FakeUserRepository,
    logins: FakeLoginRepository,
    hasher: RecordingPasswordHasher,
    tokens: FakeAccessTokenPort,
    clock: CountingClock,
    events: RecordingEventPublisher,
    failed_logins: RecordingFailedLoginObserver,
) -> LogIn:
    return LogIn(users, logins, hasher, tokens, clock, events, failed_logins, _REFRESH_LIFETIME)


async def test_unknown_email_calls_verify_with_none_exactly_once_and_raises_invalid_credentials(
    clock: FixedClock,
) -> None:
    """I-9: no account with this email. The decoy verify runs (`against=None`); nothing is written;
    the observer is told it was an unknown email, once."""
    users = FakeUserRepository()
    logins = FakeLoginRepository()
    hasher = RecordingPasswordHasher(verify_result=PasswordVerdict.MISMATCH)
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    failed_logins = RecordingFailedLoginObserver()
    use_case = _build_use_case(
        users=users,
        logins=logins,
        hasher=hasher,
        tokens=tokens,
        clock=CountingClock(clock),
        events=events,
        failed_logins=failed_logins,
    )

    with pytest.raises(InvalidCredentials):
        await use_case("ghost@example.com", "whatever-it-is", _token_hash("l1"))

    assert len(hasher.verify_calls) == 1
    assert hasher.verify_calls[0][1] is None
    assert failed_logins.unknown_email_calls == 1
    assert failed_logins.wrong_password_calls == []
    assert logins.all() == []
    assert tokens.issue_calls == []
    assert events.published == []


async def test_wrong_password_verifies_against_the_stored_hash_and_raises_invalid_credentials(
    clock: FixedClock,
) -> None:
    """I-10: byte-identical response to I-9 (proven in the dedicated comparison test below), but the
    observer is told *which* account and the verify ran against the real stored hash, not `None`."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    logins = FakeLoginRepository()
    hasher = RecordingPasswordHasher(verify_result=PasswordVerdict.MISMATCH)
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    failed_logins = RecordingFailedLoginObserver()
    use_case = _build_use_case(
        users=users,
        logins=logins,
        hasher=hasher,
        tokens=tokens,
        clock=CountingClock(clock),
        events=events,
        failed_logins=failed_logins,
    )

    with pytest.raises(InvalidCredentials):
        await use_case("alex@example.com", "the-wrong-password", _token_hash("l2"))

    assert len(hasher.verify_calls) == 1
    assert hasher.verify_calls[0][1] == user.password_hash
    assert failed_logins.wrong_password_calls == [user.id]
    assert failed_logins.unknown_email_calls == 0
    assert logins.all() == []
    assert tokens.issue_calls == []
    assert events.published == []


async def test_unknown_email_and_wrong_password_raise_the_identical_type_and_attributes(
    clock: FixedClock,
) -> None:
    """AC-9's core promise: nothing downstream — including a test comparing the two exception
    objects directly — can tell an unknown email from a wrong password by inspecting the error."""
    users_a = FakeUserRepository()
    use_case_a = _build_use_case(
        users=users_a,
        logins=FakeLoginRepository(),
        hasher=RecordingPasswordHasher(verify_result=PasswordVerdict.MISMATCH),
        tokens=FakeAccessTokenPort(),
        clock=CountingClock(clock),
        events=RecordingEventPublisher(),
        failed_logins=RecordingFailedLoginObserver(),
    )
    with pytest.raises(InvalidCredentials) as exc_a:
        await use_case_a("ghost@example.com", "whatever-it-is", _token_hash("l3"))

    users_b = FakeUserRepository()
    await _seed_user(users_b)
    use_case_b = _build_use_case(
        users=users_b,
        logins=FakeLoginRepository(),
        hasher=RecordingPasswordHasher(verify_result=PasswordVerdict.MISMATCH),
        tokens=FakeAccessTokenPort(),
        clock=CountingClock(clock),
        events=RecordingEventPublisher(),
        failed_logins=RecordingFailedLoginObserver(),
    )
    with pytest.raises(InvalidCredentials) as exc_b:
        await use_case_b("alex@example.com", "the-wrong-password", _token_hash("l4"))

    assert type(exc_a.value) is type(exc_b.value) is InvalidCredentials
    assert exc_a.value.args == exc_b.value.args == ()
    assert vars(exc_a.value) == vars(exc_b.value) == {}


async def test_match_needs_rehash_replaces_the_hash_saves_the_user_and_publishes_rehashed_then_loggedin(
    clock: FixedClock,
) -> None:
    """I-11: the hash is replaced in the same unit of work, before the new `Login` is even started."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    logins = FakeLoginRepository()
    new_hash = PasswordHash(value="$argon2id$v=19$m=131072,t=4,p=4$c2FsdDI$bmV3aGFzaA")
    hasher = RecordingPasswordHasher(
        verify_result=PasswordVerdict.MATCH_NEEDS_REHASH, hash_result=new_hash
    )
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    failed_logins = RecordingFailedLoginObserver()
    counting_clock = CountingClock(clock)
    use_case = _build_use_case(
        users=users,
        logins=logins,
        hasher=hasher,
        tokens=tokens,
        clock=counting_clock,
        events=events,
        failed_logins=failed_logins,
    )

    result = await use_case("alex@example.com", "correct password", _token_hash("l5"))

    assert isinstance(result, Authenticated)
    assert len(hasher.hash_calls) == 1
    refetched = await users.get(user.id)
    assert refetched.password_hash == new_hash
    assert logins.all() == [result.login]
    assert tokens.issue_calls == [(user.id, clock.now())]
    assert events.published == [
        UserPasswordRehashed(user_id=user.id, occurred_at=clock.now()),
        LoggedIn(user_id=user.id, login_id=result.login.id, occurred_at=clock.now()),
    ]
    assert failed_logins.unknown_email_calls == 0
    assert failed_logins.wrong_password_calls == []
    assert counting_clock.calls == 1


async def test_plain_match_does_not_rehash_and_publishes_only_loggedin(clock: FixedClock) -> None:
    """The contrast to I-11: an ordinary match never calls `hash` and never publishes
    `UserPasswordRehashed` — pairing the previous test's positive with this one's absence."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    logins = FakeLoginRepository()
    hasher = RecordingPasswordHasher(verify_result=PasswordVerdict.MATCH)
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    failed_logins = RecordingFailedLoginObserver()
    use_case = _build_use_case(
        users=users,
        logins=logins,
        hasher=hasher,
        tokens=tokens,
        clock=CountingClock(clock),
        events=events,
        failed_logins=failed_logins,
    )

    result = await use_case("alex@example.com", "correct password", _token_hash("l6"))

    assert hasher.hash_calls == []
    assert events.published == [
        LoggedIn(user_id=user.id, login_id=result.login.id, occurred_at=clock.now())
    ]


async def test_malformed_email_on_login_raises_invalid_email_with_zero_hasher_calls(
    clock: FixedClock,
) -> None:
    """I-12: a statement about the input, not about any account — checked before the hasher, the
    repositories or the observer are ever touched."""
    users = FakeUserRepository()
    logins = FakeLoginRepository()
    hasher = RecordingPasswordHasher()
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    failed_logins = RecordingFailedLoginObserver()
    use_case = _build_use_case(
        users=users,
        logins=logins,
        hasher=hasher,
        tokens=tokens,
        clock=CountingClock(clock),
        events=events,
        failed_logins=failed_logins,
    )

    with pytest.raises(InvalidEmailAddress) as exc_info:
        await use_case("not-an-email", "whatever-it-is", _token_hash("l7"))

    assert exc_info.value.reason is InvalidEmailReason.AT_SIGN_COUNT
    assert hasher.hash_calls == []
    assert hasher.verify_calls == []
    assert failed_logins.unknown_email_calls == 0
    assert failed_logins.wrong_password_calls == []
    assert logins.all() == []
