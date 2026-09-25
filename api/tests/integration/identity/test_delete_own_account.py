"""Application tests for `DeleteOwnAccount` (T9, RED, slice 2.2, AC-12, S-41, S-42).

**Verify before anything is read for deletion.** `_OrderRecordingPasswordHasher` and
`_OrderRecordingAccountData` append to one shared list, so the happy path proves `hasher.verify`
happened strictly before `EraseAccount`'s own first read (`files_of_account`) rather than merely
happening to pass. The refusal paths (wrong password, a hashing failure) prove the stronger claim
directly: `accounts.files_of_account_calls == []` — `EraseAccount` was never reached at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tailorcraft.application.identity.delete_own_account import DeleteOwnAccount
from tailorcraft.application.retention.erase_account import EraseAccount
from tailorcraft.domain.identity.errors import (
    InvalidCredentials,
    PasswordHashingFailed,
    UserNotFound,
)
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    Password,
    PasswordHash,
    PasswordVerdict,
    UserId,
)
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType
from tailorcraft.domain.retention.value_objects import AccountErasureReport
from tailorcraft.domain.shared.files import FileRef
from tests.integration.fakes import (
    FakeAccountDataPort,
    FakeUserRepository,
    InMemoryFileStore,
    RecordingPasswordHasher,
)

_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZWhhc2g")


class _OrderRecordingPasswordHasher(RecordingPasswordHasher):
    def __init__(
        self, order: list[str], *, verify_result: PasswordVerdict = PasswordVerdict.MATCH
    ) -> None:
        super().__init__(verify_result=verify_result)
        self._order = order

    async def verify(self, password: Password, against: PasswordHash | None) -> PasswordVerdict:
        result = await super().verify(password, against)
        self._order.append("verify")
        return result


class _OrderRecordingAccountData(FakeAccountDataPort):
    def __init__(
        self, order: list[str], files_by_user: dict[UserId, Sequence[FileRef]] | None = None
    ) -> None:
        super().__init__(files_by_user)
        self._order = order

    async def files_of_account(self, user_id: UserId) -> Sequence[FileRef]:
        result = await super().files_of_account(user_id)
        self._order.append("files_of_account")
        return result


async def _seed_user(users: FakeUserRepository, *, email: str = "alex@example.com") -> User:
    user = User.register_with_password(
        users.next_identity(), EmailAddress.parse(email), _HASH, at=datetime(2026, 9, 4, tzinfo=UTC)
    )
    user.release_events()
    await users.add(user)
    return user


async def test_the_correct_password_verifies_before_erase_account_reads_and_returns_its_report() -> (
    None
):
    users = FakeUserRepository()
    user = await _seed_user(users)
    order: list[str] = []
    hasher = _OrderRecordingPasswordHasher(order, verify_result=PasswordVerdict.MATCH)
    ref = FileRef.for_base_cv(BaseCvId(value=uuid4()), CvContentType.PDF)
    accounts = _OrderRecordingAccountData(order, {user.id: [ref]})
    files = InMemoryFileStore()
    await files.put(ref, b"bytes")
    erase_account = EraseAccount(accounts, files)
    use_case = DeleteOwnAccount(users, hasher, erase_account)

    report = await use_case(user.id, Password.from_input("correct horse battery staple"))

    assert report == AccountErasureReport(base_cvs=1, files_unlinked=1, unlink_failures=())
    assert order[0] == "verify"
    assert order.index("verify") < order.index("files_of_account")
    assert hasher.verify_calls == [
        (Password.from_input("correct horse battery staple"), user.password_hash)
    ]


async def test_a_gone_user_raises_user_not_found() -> None:
    users = FakeUserRepository()  # empty: no such user
    hasher = RecordingPasswordHasher()
    accounts = FakeAccountDataPort({})
    files = InMemoryFileStore()
    erase_account = EraseAccount(accounts, files)
    use_case = DeleteOwnAccount(users, hasher, erase_account)

    with pytest.raises(UserNotFound):
        await use_case(UserId(value=uuid4()), Password.from_input("whatever"))

    assert hasher.verify_calls == []


async def test_the_wrong_password_raises_invalid_credentials_and_deletes_nothing() -> None:
    """S-41: 403 `password_incorrect` at the API boundary; here, `InvalidCredentials`, and
    `EraseAccount` is never reached — `accounts.files_of_account_calls` stays empty."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    hasher = RecordingPasswordHasher(verify_result=PasswordVerdict.MISMATCH)
    ref = FileRef.for_base_cv(BaseCvId(value=uuid4()), CvContentType.PDF)
    accounts = FakeAccountDataPort({user.id: [ref]})
    files = InMemoryFileStore()
    await files.put(ref, b"bytes")
    erase_account = EraseAccount(accounts, files)
    use_case = DeleteOwnAccount(users, hasher, erase_account)

    with pytest.raises(InvalidCredentials):
        await use_case(user.id, Password.from_input("wrong password"))

    assert hasher.verify_calls == [(Password.from_input("wrong password"), user.password_hash)]
    assert accounts.files_of_account_calls == []
    assert accounts.delete_account_calls == []
    assert files.delete_calls == []


async def test_a_password_hashing_failure_propagates_exactly_and_deletes_nothing() -> None:
    """AC-12: `PasswordHashingFailed` propagates (→ 503 at the boundary) with nothing deleted."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    hasher = RecordingPasswordHasher(verify_raises=PasswordHashingFailed())
    ref = FileRef.for_base_cv(BaseCvId(value=uuid4()), CvContentType.PDF)
    accounts = FakeAccountDataPort({user.id: [ref]})
    files = InMemoryFileStore()
    await files.put(ref, b"bytes")
    erase_account = EraseAccount(accounts, files)
    use_case = DeleteOwnAccount(users, hasher, erase_account)

    with pytest.raises(PasswordHashingFailed):
        await use_case(user.id, Password.from_input("whatever"))

    assert accounts.files_of_account_calls == []
    assert accounts.delete_account_calls == []
    assert files.delete_calls == []
