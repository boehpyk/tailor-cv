"""Application tests for `DeleteOwnAccount`'s credential re-check (slice 2.5, T13 RED — AC-14, V-51).

Against today's `DeleteOwnAccount` (a change to existing code). The shape is `LogIn`'s: verify takes
~50 ms off the loop, a reset can commit inside it, and an account must not be erased on the strength
of a password the reset just replaced (a stolen old password plus a victim's reset is otherwise a
deletion). The race is staged as in `test_log_in_credential_recheck.py`: the stored row is replaced by
a different instance carrying the new hash while the use case holds the old aggregate; the fake's
`confirm_credential_unchanged` is the faithful one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

import pytest

from tailorcraft.application.identity.delete_own_account import DeleteOwnAccount
from tailorcraft.application.retention.erase_account import EraseAccount
from tailorcraft.domain.identity.errors import InvalidCredentials
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    Password,
    PasswordHash,
    PasswordVerdict,
    UserId,
)
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeAccountDataPort,
    InMemoryFileStore,
    LoggingPasswordHasher,
    LoggingUserRepository,
    commit_a_reset_elsewhere,
)

_OLD_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$b2xkaGFzaA")
_RESET_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$cmVzZXRlZA")


class _OrderedAccountData(FakeAccountDataPort):
    def __init__(self, log: list[str], user_id: UserId) -> None:
        super().__init__({user_id: []})
        self._log = log

    async def files_of_account(self, user_id: UserId) -> Sequence[FileRef]:
        self._log.append("accounts.files_of_account")
        return await super().files_of_account(user_id)


class _Rig:
    def __init__(self, log: list[str], users: LoggingUserRepository, user: User) -> None:
        self.log = log
        self.users = users
        self.user = user


async def _setup(
    clock: FixedClock, *, reset_in_flight: bool
) -> tuple[_Rig, _OrderedAccountData, DeleteOwnAccount]:
    log: list[str] = []
    users = LoggingUserRepository(log)
    user = User.register_with_password(
        users.next_identity(),
        EmailAddress.parse("alex@example.com"),
        _OLD_HASH,
        clock.now() - timedelta(days=1),
    )
    user.release_events()
    await users.add(user)

    async def reset_commits() -> None:
        await commit_a_reset_elsewhere(users, user, _RESET_HASH, clock.now())

    hasher = LoggingPasswordHasher(
        log,
        verify_result=PasswordVerdict.MATCH,
        after_verify=reset_commits if reset_in_flight else None,
    )
    accounts = _OrderedAccountData(log, user.id)
    use_case = DeleteOwnAccount(users, hasher, EraseAccount(accounts, InMemoryFileStore()))
    log.clear()
    return _Rig(log, users, user), accounts, use_case


async def test_a_credential_changed_during_the_verify_refuses_the_deletion_and_erases_nothing(
    clock: FixedClock,
) -> None:
    """V-51: 403 `password_incorrect` at the route; here `InvalidCredentials`, and `EraseAccount` is
    never reached."""
    rig, accounts, use_case = await _setup(clock, reset_in_flight=True)

    with pytest.raises(InvalidCredentials):
        await use_case(rig.user.id, Password.from_input("the old password"))

    assert accounts.files_of_account_calls == []
    assert accounts.delete_account_calls == []
    assert (await rig.users.get(rig.user.id)).password_hash == _RESET_HASH


async def test_the_recheck_runs_once_with_the_seen_hash_between_the_verify_and_the_erasure(
    clock: FixedClock,
) -> None:
    """AC-14's order: verify, then the re-check, then `EraseAccount`'s first read."""
    rig, _accounts, use_case = await _setup(clock, reset_in_flight=False)

    await use_case(rig.user.id, Password.from_input("correct password"))

    assert rig.users.confirm_calls == [(rig.user.id, _OLD_HASH)]
    assert rig.log.index("hasher.verify") < rig.log.index("users.confirm_credential_unchanged")
    assert rig.log.index("users.confirm_credential_unchanged") < rig.log.index(
        "accounts.files_of_account"
    )
