"""Application tests for `ResetPassword` (slice 2.5, T13 RED — AC-12, V-45 … V-48).

A reset link's token and a new password replace an account's credential and **revoke every device**.
The order is the design (§0.8): the reset is read unlocked only to learn whose it is, the **user** row
is locked first, and only then is the reset re-found and locked — everything that takes both takes the
user first, so a reset and an account erasure queue on one row instead of deadlocking on two. The shared
call log pins that order across the three stores. Two refusals matter as much as the happy path:

- **A policy refusal leaves the token usable** (V-46): checked after the locks and before any write,
  and nothing is removed on that path, so the user can retry with the same link.
- **A hashing failure writes nothing** (V-48).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tailorcraft.application.identity.reset_password import ResetPassword
from tailorcraft.domain.identity.errors import (
    PasswordHashingFailed,
    ResetTokenInvalid,
    WeakPassword,
)
from tailorcraft.domain.identity.events import PasswordChangedByReset
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.password_reset import PasswordReset
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    PasswordHash,
    PasswordPolicy,
    TokenHash,
    TokenRefusal,
    UserId,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    CountingClock,
    FakePasswordResetRepository,
    LoggingEventPublisher,
    LoggingLoginRepository,
    LoggingPasswordHasher,
    LoggingUserRepository,
)

_TTL = timedelta(minutes=60)
_OLD_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$b2xkaGFzaA")
_NEW_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$bmV3aGFzaA")
_EMAIL = EmailAddress.parse("alex@example.com")
_TOKEN_HASH = TokenHash(value="e" * 64)
_UNKNOWN_TOKEN_HASH = TokenHash(value="f" * 64)
_GOOD_PASSWORD = "a brand new passphrase"


class _Rig:
    def __init__(
        self,
        clock: FixedClock,
        *,
        lose_lock: bool = False,
        hash_raises: Exception | None = None,
    ) -> None:
        self.log: list[str] = []
        self.users = LoggingUserRepository(self.log)
        self.resets = FakePasswordResetRepository(self.log, lose_lock=lose_lock)
        self.logins = LoggingLoginRepository(self.log)
        self.hasher = LoggingPasswordHasher(
            self.log, hash_result=_NEW_HASH, hash_raises=hash_raises
        )
        self.events = LoggingEventPublisher(self.log)
        self.clock = CountingClock(clock)
        self.use_case = ResetPassword(
            self.users,
            self.resets,
            self.logins,
            self.hasher,
            self.clock,
            self.events,
            PasswordPolicy(),
        )

    async def seed_user(self, at: datetime, email: EmailAddress = _EMAIL) -> User:
        user = User.register_with_password(self.users.next_identity(), email, _OLD_HASH, at)
        user.release_events()
        await self.users.add(user)
        return user

    def seed_issued_reset(self, user_id: UserId, at: datetime) -> PasswordReset:
        r = PasswordReset.request(self.resets.next_identity(), _EMAIL, at, _TTL)
        r.issue(user_id, _TOKEN_HASH, at)
        self.resets.seed(r)
        return r

    async def seed_logins(self, user_id: UserId, count: int, at: datetime) -> None:
        for n in range(count):
            login = Login.start(
                self.logins.next_identity(),
                user_id,
                TokenHash(value=f"{user_id.value.int + n:064x}"[-64:]),
                at,
                timedelta(days=30),
            )
            login.release_events()
            await self.logins.add(login)

    def settle(self) -> None:
        """Drop the seeding from the call log: the use case's sequence is what is asserted."""
        self.log.clear()


def _in_order(log: list[str], expected: list[str]) -> bool:
    positions = [log.index(name) for name in expected if name in log]
    return len(positions) == len(expected) and positions == sorted(positions)


async def test_a_valid_reset_replaces_the_hash_revokes_every_login_and_spends_the_resets(
    clock: FixedClock,
) -> None:
    """V-47 / AC-12: the credential, the devices and the links, in one go."""
    rig = _Rig(clock)
    user = await rig.seed_user(clock.now())
    other = await rig.seed_user(clock.now(), EmailAddress.parse("someone-else@example.com"))
    rig.seed_issued_reset(user.id, clock.now())
    await rig.seed_logins(user.id, 2, clock.now())
    await rig.seed_logins(other.id, 1, clock.now())
    rig.settle()

    await rig.use_case(_TOKEN_HASH, _GOOD_PASSWORD)

    refreshed = await rig.users.get(user.id)
    assert refreshed.password_hash == _NEW_HASH
    assert refreshed.password_updated_at == clock.now()
    assert [login.user_id for login in rig.logins.all()] == [other.id]
    assert rig.resets.all() == []
    assert rig.events.published == [
        PasswordChangedByReset(user_id=user.id, logins_revoked=2, occurred_at=clock.now())
    ]


async def test_the_user_is_locked_before_the_reset_and_events_follow_every_write(
    clock: FixedClock,
) -> None:
    """§0.8's lock order and AC-12's step order, across the three stores and the hasher."""
    rig = _Rig(clock)
    user = await rig.seed_user(clock.now())
    rig.seed_issued_reset(user.id, clock.now())
    rig.settle()

    await rig.use_case(_TOKEN_HASH, _GOOD_PASSWORD)

    assert _in_order(
        rig.log,
        [
            "resets.find_by_token_hash",
            "users.get_for_update",
            "resets.lock_by_token_hash",
            "hasher.hash",
            "logins.remove_all_for_user",
            "users.save",
            "resets.remove_all_for_user",
            "events.publish",
        ],
    )
    assert rig.log.count("hasher.hash") == 1


async def test_an_unknown_token_is_refused_as_unknown_and_nothing_is_written(
    clock: FixedClock,
) -> None:
    """V-45."""
    rig = _Rig(clock)
    user = await rig.seed_user(clock.now())
    rig.seed_issued_reset(user.id, clock.now())
    await rig.seed_logins(user.id, 1, clock.now())
    rig.settle()

    with pytest.raises(ResetTokenInvalid) as refused:
        await rig.use_case(_UNKNOWN_TOKEN_HASH, _GOOD_PASSWORD)

    assert refused.value.reason is TokenRefusal.UNKNOWN
    assert "hasher.hash" not in rig.log
    assert "users.save" not in rig.log
    assert "logins.remove_all_for_user" not in rig.log
    assert len(rig.resets.all()) == 1
    assert len(rig.logins.all()) == 1
    assert rig.events.published == []


async def test_a_reset_used_or_superseded_between_the_two_reads_is_refused_after_the_user_lock(
    clock: FixedClock,
) -> None:
    """V-45 / V-49: `lock_by_token_hash` finds nothing — a concurrent confirm won. The user lock was
    already taken (the order is unconditional), and nothing was hashed or written."""
    rig = _Rig(clock, lose_lock=True)
    user = await rig.seed_user(clock.now())
    rig.seed_issued_reset(user.id, clock.now())
    await rig.seed_logins(user.id, 1, clock.now())
    rig.settle()

    with pytest.raises(ResetTokenInvalid) as refused:
        await rig.use_case(_TOKEN_HASH, _GOOD_PASSWORD)

    assert refused.value.reason is TokenRefusal.UNKNOWN
    assert _in_order(
        rig.log, ["resets.find_by_token_hash", "users.get_for_update", "resets.lock_by_token_hash"]
    )
    assert "hasher.hash" not in rig.log
    assert "users.save" not in rig.log
    assert len(rig.logins.all()) == 1


async def test_an_expired_token_removes_the_row_then_is_refused_as_expired(
    clock: FixedClock,
) -> None:
    """V-45: the refusal that writes — and the account is untouched."""
    rig = _Rig(clock)
    user = await rig.seed_user(clock.now())
    rig.seed_issued_reset(user.id, clock.now() - _TTL)
    await rig.seed_logins(user.id, 1, clock.now())
    rig.settle()

    with pytest.raises(ResetTokenInvalid) as refused:
        await rig.use_case(_TOKEN_HASH, _GOOD_PASSWORD)

    assert refused.value.reason is TokenRefusal.EXPIRED
    assert rig.resets.all() == []
    assert "hasher.hash" not in rig.log
    assert (await rig.users.get(user.id)).password_hash == _OLD_HASH
    assert len(rig.logins.all()) == 1


@pytest.mark.parametrize("password", ["short", "alex@example.com"])
async def test_a_policy_refusal_writes_nothing_and_leaves_the_token_usable(
    clock: FixedClock, password: str
) -> None:
    """V-46: refused after the locks, before the hash; the reset, the logins and the old credential
    all survive, so the same link works for a better password."""
    rig = _Rig(clock)
    user = await rig.seed_user(clock.now())
    rig.seed_issued_reset(user.id, clock.now())
    await rig.seed_logins(user.id, 2, clock.now())
    rig.settle()

    with pytest.raises(WeakPassword):
        await rig.use_case(_TOKEN_HASH, password)

    assert "hasher.hash" not in rig.log
    assert "users.save" not in rig.log
    assert "logins.remove_all_for_user" not in rig.log
    assert "resets.remove" not in rig.log
    assert "resets.remove_all_for_user" not in rig.log
    assert len(rig.resets.all()) == 1
    assert len(rig.logins.all()) == 2
    assert (await rig.users.get(user.id)).password_hash == _OLD_HASH
    assert rig.events.published == []

    # ...and the retry with the same link succeeds.
    await rig.use_case(_TOKEN_HASH, _GOOD_PASSWORD)
    assert (await rig.users.get(user.id)).password_hash == _NEW_HASH


async def test_an_empty_password_is_refused_before_any_lookup(clock: FixedClock) -> None:
    """Step 1: `Password.from_input` runs first; no store is touched."""
    rig = _Rig(clock)
    user = await rig.seed_user(clock.now())
    rig.seed_issued_reset(user.id, clock.now())
    rig.settle()

    with pytest.raises(WeakPassword):
        await rig.use_case(_TOKEN_HASH, "")

    assert rig.log == []


async def test_a_hashing_failure_propagates_with_nothing_written(clock: FixedClock) -> None:
    """V-48: the token, the logins and the old credential are all intact."""
    rig = _Rig(clock, hash_raises=PasswordHashingFailed())
    user = await rig.seed_user(clock.now())
    rig.seed_issued_reset(user.id, clock.now())
    await rig.seed_logins(user.id, 2, clock.now())
    rig.settle()

    with pytest.raises(PasswordHashingFailed):
        await rig.use_case(_TOKEN_HASH, _GOOD_PASSWORD)

    assert "users.save" not in rig.log
    assert "logins.remove_all_for_user" not in rig.log
    assert len(rig.resets.all()) == 1
    assert len(rig.logins.all()) == 2
    assert (await rig.users.get(user.id)).password_hash == _OLD_HASH
    assert rig.events.published == []


async def test_a_reset_with_no_logins_records_zero_revoked(clock: FixedClock) -> None:
    """The event's count is what makes it worth reading: 0 is a forgotten password."""
    rig = _Rig(clock)
    user = await rig.seed_user(clock.now())
    rig.seed_issued_reset(user.id, clock.now())
    rig.settle()

    await rig.use_case(_TOKEN_HASH, _GOOD_PASSWORD)

    assert rig.events.published == [
        PasswordChangedByReset(user_id=user.id, logins_revoked=0, occurred_at=clock.now())
    ]


async def test_one_clock_reading_per_reset(clock: FixedClock) -> None:
    rig = _Rig(clock)
    user = await rig.seed_user(clock.now())
    rig.seed_issued_reset(user.id, clock.now())

    await rig.use_case(_TOKEN_HASH, _GOOD_PASSWORD)

    assert rig.clock.calls == 1
