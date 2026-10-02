"""Application tests for `ConfirmRegistration` (slice 2.5, T13 RED — AC-11, V-32 … V-37).

A confirmation link's token turns a pending registration into a `User`. **It signs nobody in**: the
class is not given a `LoginRepository` or an `AccessTokenPort` (§0.6 — otherwise an attacker who
registered your address with their password has you signed into their account the moment you
click). That absence is proven structurally (green against the skeleton, red under the mutation
"inject a `LoginRepository`") and paired with the positives that go red: the `User` is built from the
row, the row is removed, `UserRegistered` is published.

A refusal that writes (an expired row is deleted) **raises after the write**; the route commits it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tailorcraft.application.identity.confirm_registration import ConfirmRegistration
from tailorcraft.domain.identity.errors import ConfirmationTokenInvalid, EmailAlreadyRegistered
from tailorcraft.domain.identity.events import UserRegistered
from tailorcraft.domain.identity.pending_registration import PendingRegistration
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    PasswordHash,
    TokenHash,
    TokenRefusal,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    CountingClock,
    FakePendingRegistrationRepository,
    LoggingEventPublisher,
    LoggingUserRepository,
)
from tests.integration.identity.structure import constructor_dependency_names, module_references

_TTL = timedelta(hours=24)
_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZWhhc2g")
_EMAIL = EmailAddress.parse("alex@example.com")
_TOKEN_HASH = TokenHash(value="c" * 64)
_OTHER_TOKEN_HASH = TokenHash(value="d" * 64)


class _Rig:
    def __init__(self, clock: FixedClock) -> None:
        self.log: list[str] = []
        self.pending = FakePendingRegistrationRepository(self.log)
        self.users = LoggingUserRepository(self.log)
        self.events = LoggingEventPublisher(self.log)
        self.clock = CountingClock(clock)
        self.use_case = ConfirmRegistration(self.pending, self.users, self.clock, self.events)

    def seed_issued(self, requested_at: datetime) -> PendingRegistration:
        p = PendingRegistration.request(
            self.pending.next_identity(), _EMAIL, _HASH, requested_at, _TTL
        )
        p.issue(_TOKEN_HASH, requested_at)
        self.pending.seed(p)
        return p


def test_the_constructor_is_given_no_login_repository_and_no_access_token_port() -> None:
    """AC-11: confirming cannot create a `Login` — it holds nothing to create one with."""
    dependencies = constructor_dependency_names(ConfirmRegistration)
    assert "LoginRepository" not in dependencies
    assert "AccessTokenPort" not in dependencies


def test_the_module_never_names_a_login_or_an_access_token() -> None:
    assert (
        module_references(ConfirmRegistration, {"Login", "LoginRepository", "AccessTokenPort"})
        == set()
    )


async def test_a_valid_token_registers_the_user_from_the_row_removes_it_and_publishes_the_event(
    clock: FixedClock,
) -> None:
    """V-35."""
    rig = _Rig(clock)
    rig.seed_issued(clock.now() - timedelta(hours=1))

    user_id = await rig.use_case(_TOKEN_HASH)

    (user,) = rig.users.all()
    assert user.id == user_id
    assert user.email == _EMAIL
    assert user.password_hash == _HASH
    assert user.created_at == clock.now()
    assert user.password_updated_at == clock.now()
    assert rig.pending.all() == []
    assert rig.events.published == [UserRegistered(user_id=user_id, occurred_at=clock.now())]


async def test_the_user_is_inserted_before_the_row_is_removed_and_events_follow_the_writes(
    clock: FixedClock,
) -> None:
    """§0.8's order: pending row, then the `User` insert, then the delete — and publish last."""
    rig = _Rig(clock)
    rig.seed_issued(clock.now())

    await rig.use_case(_TOKEN_HASH)

    assert rig.log == [
        "pending.lock_by_token_hash",
        "users.add",
        "pending.remove",
        "events.publish",
    ]


async def test_confirming_creates_no_login(clock: FixedClock) -> None:
    """AC-11's behavioural half, so the structural half is not the only witness: the user exists, and
    no sign-in side effect is observable on the only port that could carry one (the event stream
    holds only `UserRegistered`)."""
    rig = _Rig(clock)
    rig.seed_issued(clock.now())

    await rig.use_case(_TOKEN_HASH)

    assert len(rig.users.all()) == 1
    assert [type(e) for e in rig.events.published] == [UserRegistered]


async def test_an_unknown_token_is_refused_as_unknown_and_nothing_is_written(
    clock: FixedClock,
) -> None:
    """V-33: never issued, superseded, or already used — one answer."""
    rig = _Rig(clock)
    rig.seed_issued(clock.now())

    with pytest.raises(ConfirmationTokenInvalid) as refused:
        await rig.use_case(_OTHER_TOKEN_HASH)

    assert refused.value.reason is TokenRefusal.UNKNOWN
    assert rig.users.all() == []
    assert len(rig.pending.all()) == 1
    assert rig.events.published == []


async def test_an_expired_token_removes_the_row_then_is_refused_as_expired(
    clock: FixedClock,
) -> None:
    """V-34: the refusal that writes — the removal happens, then the error is raised."""
    rig = _Rig(clock)
    rig.seed_issued(clock.now() - _TTL)

    with pytest.raises(ConfirmationTokenInvalid) as refused:
        await rig.use_case(_TOKEN_HASH)

    assert refused.value.reason is TokenRefusal.EXPIRED
    assert rig.pending.all() == []
    assert rig.users.all() == []
    assert rig.events.published == []
    assert "pending.remove" in rig.log


async def test_an_address_that_became_an_account_meanwhile_removes_the_row_and_propagates(
    clock: FixedClock,
) -> None:
    """V-37: `users.add` is the uniqueness check; the pending row has no future, and the error
    reaches the route (409 `email_already_registered`)."""
    rig = _Rig(clock)
    rig.seed_issued(clock.now())
    existing = User.register_with_password(rig.users.next_identity(), _EMAIL, _HASH, clock.now())
    existing.release_events()
    await rig.users.add(existing)

    with pytest.raises(EmailAlreadyRegistered):
        await rig.use_case(_TOKEN_HASH)

    assert rig.pending.all() == []
    assert len(rig.users.all()) == 1
    assert rig.events.published == []


async def test_one_clock_reading_per_confirmation(clock: FixedClock) -> None:
    rig = _Rig(clock)
    rig.seed_issued(clock.now())

    await rig.use_case(_TOKEN_HASH)

    assert rig.clock.calls == 1
