"""Application tests for `RegisterUser` (T13, RED).

Covers AC-8 and the registration half of the failure contract (I-1 … I-6) against **in-memory
repository fakes** and a **recording hasher** (`tests/integration/fakes.py`) — the split 1.6's T9
made for the purge use case: database truths (the real unique-index violation, I-5's exact SQL path)
wait for T31, against a real Postgres. This file exercises what `RegisterUser.__call__` itself is
responsible for, per its own skeleton docstring's numbered flow.

Every assertion below states what `RegisterUser.__call__` **should** do — never what the (currently
`NotImplementedError`) code was observed doing. Every failure test pairs the absence assertion
("zero hasher calls") with a positive, discriminating one (the exact error type and its reason/bounds)
in the same test, per the two traps at the top of task-list.md.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from tailorcraft.application.identity.register_user import RegisterUser
from tailorcraft.application.identity.results import Authenticated
from tailorcraft.domain.identity.errors import (
    EmailAlreadyRegistered,
    InvalidEmailAddress,
    WeakPassword,
)
from tailorcraft.domain.identity.events import LoggedIn, UserRegistered
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    InvalidEmailReason,
    PasswordHash,
    PasswordPolicy,
    TokenHash,
    WeakPasswordReason,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    CountingClock,
    FakeAccessTokenPort,
    FakeLoginRepository,
    FakeUserRepository,
    RecordingEventPublisher,
    RecordingPasswordHasher,
)

_REFRESH_LIFETIME = timedelta(days=30)


def _token_hash(seed: str) -> TokenHash:
    """A syntactically valid `TokenHash` (64 lowercase hex characters) distinguishable by `seed`."""
    return TokenHash(value=hashlib.sha256(seed.encode()).hexdigest())


def _build_use_case(
    *,
    users: FakeUserRepository,
    logins: FakeLoginRepository,
    hasher: RecordingPasswordHasher,
    tokens: FakeAccessTokenPort,
    clock: CountingClock,
    events: RecordingEventPublisher,
    policy: PasswordPolicy | None = None,
) -> RegisterUser:
    return RegisterUser(
        users,
        logins,
        hasher,
        tokens,
        clock,
        events,
        policy or PasswordPolicy(),
        _REFRESH_LIFETIME,
    )


async def test_malformed_email_raises_before_hashing_and_writes_nothing(clock: FixedClock) -> None:
    """I-1: no `@` at all. `EmailAddress.parse` raises before the hasher, the token port or the
    repositories are ever touched."""
    users = FakeUserRepository()
    logins = FakeLoginRepository()
    hasher = RecordingPasswordHasher()
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    use_case = _build_use_case(
        users=users,
        logins=logins,
        hasher=hasher,
        tokens=tokens,
        clock=CountingClock(clock),
        events=events,
    )

    with pytest.raises(InvalidEmailAddress) as exc_info:
        await use_case("not-an-email", "ValidPassw0rd!", _token_hash("t1"))

    assert exc_info.value.reason is InvalidEmailReason.AT_SIGN_COUNT
    assert hasher.hash_calls == []
    assert tokens.issue_calls == []
    assert users.all() == []
    assert logins.all() == []
    assert events.published == []


async def test_password_too_short_raises_before_hashing(clock: FixedClock) -> None:
    """I-2: fewer than 12 code points after NFKC."""
    users = FakeUserRepository()
    logins = FakeLoginRepository()
    hasher = RecordingPasswordHasher()
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    use_case = _build_use_case(
        users=users,
        logins=logins,
        hasher=hasher,
        tokens=tokens,
        clock=CountingClock(clock),
        events=events,
    )

    with pytest.raises(WeakPassword) as exc_info:
        await use_case("alex@example.com", "x" * 11, _token_hash("t2"))

    assert exc_info.value.reason is WeakPasswordReason.TOO_SHORT
    assert exc_info.value.min_length == 12
    assert exc_info.value.max_length == 128
    assert hasher.hash_calls == []
    assert tokens.issue_calls == []
    assert users.all() == []
    assert logins.all() == []


async def test_password_over_the_registration_policy_bound_raises_before_hashing(
    clock: FixedClock,
) -> None:
    """I-3, the 128 policy bound: 129 code points passes `Password.from_input`'s 1024 input bound
    but is refused by `PasswordPolicy` naming 128."""
    users = FakeUserRepository()
    logins = FakeLoginRepository()
    hasher = RecordingPasswordHasher()
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    use_case = _build_use_case(
        users=users,
        logins=logins,
        hasher=hasher,
        tokens=tokens,
        clock=CountingClock(clock),
        events=events,
    )

    with pytest.raises(WeakPassword) as exc_info:
        await use_case("alex@example.com", "x" * 129, _token_hash("t3"))

    assert exc_info.value.reason is WeakPasswordReason.TOO_LONG
    assert exc_info.value.min_length == 12
    assert exc_info.value.max_length == 128
    assert hasher.hash_calls == []
    assert tokens.issue_calls == []
    assert users.all() == []


async def test_password_over_the_1025_input_bound_raises_before_hashing(clock: FixedClock) -> None:
    """I-3, the 1024 input bound: `Password.from_input` refuses a megabyte "password" before
    `PasswordPolicy` (naming 12/128) is ever consulted — a different pair of bounds in the same
    error type is the discriminating assertion."""
    users = FakeUserRepository()
    logins = FakeLoginRepository()
    hasher = RecordingPasswordHasher()
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    use_case = _build_use_case(
        users=users,
        logins=logins,
        hasher=hasher,
        tokens=tokens,
        clock=CountingClock(clock),
        events=events,
    )

    with pytest.raises(WeakPassword) as exc_info:
        await use_case("alex@example.com", "x" * 1025, _token_hash("t4"))

    assert exc_info.value.reason is WeakPasswordReason.TOO_LONG
    assert exc_info.value.min_length == 1
    assert exc_info.value.max_length == 1024
    assert hasher.hash_calls == []
    assert tokens.issue_calls == []
    assert users.all() == []


async def test_password_matching_the_email_raises_before_hashing(clock: FixedClock) -> None:
    """I-4: case-insensitive match against the whole address."""
    users = FakeUserRepository()
    logins = FakeLoginRepository()
    hasher = RecordingPasswordHasher()
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    use_case = _build_use_case(
        users=users,
        logins=logins,
        hasher=hasher,
        tokens=tokens,
        clock=CountingClock(clock),
        events=events,
    )

    with pytest.raises(WeakPassword) as exc_info:
        await use_case("alex@example.com", "Alex@Example.com", _token_hash("t5"))

    assert exc_info.value.reason is WeakPasswordReason.MATCHES_EMAIL
    assert exc_info.value.min_length == 12
    assert exc_info.value.max_length == 128
    assert hasher.hash_calls == []
    assert tokens.issue_calls == []
    assert users.all() == []


async def test_successful_registration_creates_one_user_and_one_login_issues_a_token_and_publishes_exactly_two_events(
    clock: FixedClock,
) -> None:
    """AC-8's happy path: one `User`, one `Login` at generation 1 expiring at `now + lifetime`, one
    issued access token, and exactly `[UserRegistered, LoggedIn]` published — with one `clock.now()`
    for the whole call."""
    users = FakeUserRepository()
    logins = FakeLoginRepository()
    hasher = RecordingPasswordHasher()
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    counting_clock = CountingClock(clock)
    use_case = _build_use_case(
        users=users,
        logins=logins,
        hasher=hasher,
        tokens=tokens,
        clock=counting_clock,
        events=events,
    )

    result = await use_case("alex@example.com", "correct horse battery", _token_hash("t6"))

    assert isinstance(result, Authenticated)
    assert users.all() == [result.user]
    assert result.user.email == EmailAddress.parse("alex@example.com")
    assert logins.all() == [result.login]
    assert result.login.generation == 1
    assert result.login.user_id == result.user.id
    assert result.login.expires_at == clock.now() + _REFRESH_LIFETIME
    assert tokens.issue_calls == [(result.user.id, clock.now())]
    assert result.access_token.token == "fake-access-token-1"  # the fake's one and only issuance
    assert len(hasher.hash_calls) == 1
    assert hasher.hash_calls[0].value == "correct horse battery"
    assert events.published == [
        UserRegistered(user_id=result.user.id, occurred_at=clock.now()),
        LoggedIn(user_id=result.user.id, login_id=result.login.id, occurred_at=clock.now()),
    ]
    assert counting_clock.calls == 1


async def test_duplicate_email_case_and_whitespace_variant_raises_after_exactly_one_hash_call_and_writes_nothing_new(
    clock: FixedClock,
) -> None:
    """I-5: the unique index (here, `FakeUserRepository.add`'s check) is what refuses the second
    registration, and it refuses **after** hashing (technical plan §0.4) — the insert is the check,
    never a look-up first. No second user, no login."""
    users = FakeUserRepository()
    logins = FakeLoginRepository()
    hasher = RecordingPasswordHasher()
    tokens = FakeAccessTokenPort()
    events = RecordingEventPublisher()
    counting_clock = CountingClock(clock)
    use_case = _build_use_case(
        users=users,
        logins=logins,
        hasher=hasher,
        tokens=tokens,
        clock=counting_clock,
        events=events,
    )

    # Seed an existing registration directly, bypassing the use case so the hasher's call log below
    # reflects only the second (failing) attempt.
    existing = User.register_with_password(
        users.next_identity(),
        EmailAddress.parse("alex@example.com"),
        PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$b2xkaGFzaA"),
        at=datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC),
    )
    existing.release_events()  # a user rehydrated from storage carries no buffered events
    await users.add(existing)

    with pytest.raises(EmailAlreadyRegistered):
        await use_case(" Alex@Example.com ", "a totally different pw", _token_hash("t7"))

    assert len(hasher.hash_calls) == 1
    assert users.all() == [existing]
    assert logins.all() == []
    assert events.published == []
