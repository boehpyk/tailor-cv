"""Application tests for `LogIn`'s credential re-check (slice 2.5, T13 RED — AC-13, V-50, V-56).

**These run against today's `LogIn`** (no skeleton: this is a change to existing code, 2.4's AC-4
precedent). A password reset can commit inside the ~50 ms a login spends verifying. Without a re-check
the login finishes with the OLD password and its `Login` survives the reset that was meant to evict it
(§0.7). The re-check — `users.confirm_credential_unchanged(user.id, seen_hash)` after a matching
verdict and **before any write** — closes it.

**How the race is staged, honestly.** `LoggingPasswordHasher(after_verify=…)` runs
`commit_a_reset_elsewhere` once `verify` has produced its verdict: the stored row is replaced by a
*different instance* with a new hash, as another connection's committed reset would, while the use case
keeps the old aggregate it loaded. The fake's `confirm_credential_unchanged` is the faithful one (True
iff stored hash == seen); nothing is forced to `False`.

The unknown-email and wrong-password paths must make **no** such call — their timing equality is 2.1's
AC-28 and the re-check is deliberately skipped there. Those two tests pass today and guard the line.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest

from tailorcraft.application.identity.log_in import LogIn
from tailorcraft.domain.identity.errors import InvalidCredentials
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    PasswordHash,
    PasswordVerdict,
    TokenHash,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    CountingClock,
    FakeAccessTokenPort,
    LoggingEventPublisher,
    LoggingLoginRepository,
    LoggingPasswordHasher,
    LoggingUserRepository,
    RecordingFailedLoginObserver,
    commit_a_reset_elsewhere,
)

_OLD_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$b2xkaGFzaA")
_RESET_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$cmVzZXRlZA")
_REHASHED = PasswordHash(value="$argon2id$v=19$m=131072,t=4,p=4$c2FsdDI$bmV3aGFzaA")
_REFRESH_HASH = TokenHash(value=hashlib.sha256(b"refresh").hexdigest())


class _Rig:
    def __init__(
        self, clock: FixedClock, verdict: PasswordVerdict, *, reset_in_flight: bool
    ) -> None:
        self.log: list[str] = []
        self.clock_source = clock
        self.users = LoggingUserRepository(self.log)
        self.logins = LoggingLoginRepository(self.log)
        self.tokens = FakeAccessTokenPort()
        self.events = LoggingEventPublisher(self.log)
        self.failed = RecordingFailedLoginObserver()
        self.user: User | None = None
        self.hasher = LoggingPasswordHasher(
            self.log,
            verify_result=verdict,
            hash_result=_REHASHED,
            after_verify=self._reset_commits if reset_in_flight else None,
        )
        self.use_case = LogIn(
            self.users,
            self.logins,
            self.hasher,
            self.tokens,
            CountingClock(clock),
            self.events,
            self.failed,
            timedelta(days=30),
        )

    async def _reset_commits(self) -> None:
        assert self.user is not None
        mark = len(self.log)
        await commit_a_reset_elsewhere(
            self.users, self.user, _RESET_HASH, self.clock_source.now() + timedelta(seconds=1)
        )
        del self.log[mark:]  # another connection's write is not this use case's call

    async def seed_user(self) -> User:
        user = User.register_with_password(
            self.users.next_identity(),
            EmailAddress.parse("alex@example.com"),
            _OLD_HASH,
            self.clock_source.now() - timedelta(days=1),
        )
        user.release_events()
        await self.users.add(user)
        self.user = user
        self.log.clear()
        return user


@pytest.mark.parametrize("verdict", [PasswordVerdict.MATCH, PasswordVerdict.MATCH_NEEDS_REHASH])
async def test_a_credential_changed_during_the_verify_refuses_the_login_as_a_wrong_password(
    clock: FixedClock, verdict: PasswordVerdict
) -> None:
    """V-50 / V-56: the reset committed first, so the old password must not open a `Login`. Answered
    exactly as a wrong password: `InvalidCredentials`, the observer told `wrong_password(user.id)`,
    no `Login`, no token, no rehash (a rehash would write the OLD password's hash over the new one)."""
    rig = _Rig(clock, verdict, reset_in_flight=True)
    user = await rig.seed_user()

    with pytest.raises(InvalidCredentials):
        await rig.use_case("alex@example.com", "the old password", _REFRESH_HASH)

    assert rig.failed.wrong_password_calls == [user.id]
    assert rig.failed.unknown_email_calls == 0
    assert rig.logins.all() == []
    assert rig.tokens.issue_calls == []
    assert "logins.add" not in rig.log
    assert "users.save" not in rig.log
    assert rig.hasher.hash_calls == []
    assert (await rig.users.get(user.id)).password_hash == _RESET_HASH
    assert rig.events.published == []


async def test_the_recheck_runs_once_with_the_seen_hash_after_the_verify_and_before_any_write(
    clock: FixedClock,
) -> None:
    """AC-13's order for the plain-match path, from the shared log."""
    rig = _Rig(clock, PasswordVerdict.MATCH, reset_in_flight=False)
    user = await rig.seed_user()

    await rig.use_case("alex@example.com", "correct password", _REFRESH_HASH)

    assert rig.users.confirm_calls == [(user.id, _OLD_HASH)]
    assert rig.log.index("hasher.verify") < rig.log.index("users.confirm_credential_unchanged")
    assert rig.log.index("users.confirm_credential_unchanged") < rig.log.index("logins.add")


async def test_the_recheck_precedes_the_rehash_write_on_the_needs_rehash_path(
    clock: FixedClock,
) -> None:
    rig = _Rig(clock, PasswordVerdict.MATCH_NEEDS_REHASH, reset_in_flight=False)
    user = await rig.seed_user()

    await rig.use_case("alex@example.com", "correct password", _REFRESH_HASH)

    assert rig.users.confirm_calls == [(user.id, _OLD_HASH)]
    assert rig.log.index("users.confirm_credential_unchanged") < rig.log.index("users.save")
    assert rig.log.index("users.save") < rig.log.index("logins.add")


async def test_an_unknown_email_makes_no_recheck(clock: FixedClock) -> None:
    """The decoy path's timing is 2.1's AC-28: it must not gain a call the other path skips."""
    rig = _Rig(clock, PasswordVerdict.MISMATCH, reset_in_flight=False)

    with pytest.raises(InvalidCredentials):
        await rig.use_case("ghost@example.com", "whatever", _REFRESH_HASH)

    assert rig.users.confirm_calls == []


async def test_a_wrong_password_makes_no_recheck(clock: FixedClock) -> None:
    rig = _Rig(clock, PasswordVerdict.MISMATCH, reset_in_flight=False)
    await rig.seed_user()

    with pytest.raises(InvalidCredentials):
        await rig.use_case("alex@example.com", "the wrong password", _REFRESH_HASH)

    assert rig.users.confirm_calls == []
