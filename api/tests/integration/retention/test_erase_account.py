"""Application tests for `EraseAccount` (T9, RED, slice 2.2, AC-11, S-45, S-46).

**Order is the whole point again** (technical plan §0.4/§0.5, the same shape `DeleteSavedBaseCv`'s
own test file proves): `files_of_account` before `delete_account`, and every unlink only after
`delete_account` has returned. `_OrderRecordingAccountData` and `_OrderRecordingFileStore` append to
one shared list, so the order assertion is a plain list equality.

**This use case does not log** (its own docstring says so — unlink failures are *returned*, never
logged, so the entry point can turn each one into a line that also sits inside AC-49's field of
view). `test_some_unlinks_failing_are_returned_never_logged` is the one test in this file that
proves it with `caplog`, and it is paired with the discriminating positive `unlink_failures` demands
— a tuple that actually names the failing key's exception type — so the "nothing logged" half is not
merely a vacuous absence.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import pytest

from tailorcraft.application.retention.erase_account import EraseAccount
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType
from tailorcraft.domain.retention.errors import AccountNotFound
from tailorcraft.domain.retention.value_objects import AccountErasureReport
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.settings import Settings
from tests.integration.fakes import FakeAccountDataPort, InMemoryFileStore


def _numbered_ref(seed: int) -> FileRef:
    """A distinct, valid `FileRef` per call — `FileRef.for_base_cv` already guarantees uniqueness
    from a fresh `BaseCvId`, so this is just a readable name for "give me one more"."""
    return FileRef.for_base_cv(BaseCvId(value=uuid4()), CvContentType.PDF)


class _OrderRecordingAccountData(FakeAccountDataPort):
    """Appends to a shared order list on the instant each read/write actually succeeds — mirrors
    `test_delete_saved_base_cv.py`'s `_OrderRecordingBaseCvRepository`."""

    def __init__(
        self, order: list[str], files_by_user: dict[UserId, Sequence[FileRef]] | None = None
    ) -> None:
        super().__init__(files_by_user)
        self._order = order

    async def files_of_account(self, user_id: UserId) -> Sequence[FileRef]:
        result = await super().files_of_account(user_id)
        self._order.append("files_of_account")
        return result

    async def delete_account(self, user_id: UserId) -> bool:
        result = await super().delete_account(user_id)
        self._order.append("delete_account")
        return result


class _OrderRecordingFileStore(InMemoryFileStore):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    async def delete(self, ref: FileRef) -> None:
        self._order.append("file_deleted")
        await super().delete(ref)


async def test_happy_path_collects_keys_then_deletes_then_unlinks_each_in_that_order() -> None:
    user_id = UserId(value=uuid4())
    refs = [_numbered_ref(1), _numbered_ref(2), _numbered_ref(3)]
    order: list[str] = []
    accounts = _OrderRecordingAccountData(order, {user_id: refs})
    files = _OrderRecordingFileStore(order)
    for ref in refs:
        await files.put(ref, b"saved cv bytes")
    use_case = EraseAccount(accounts, files)

    report = await use_case(user_id)

    assert report == AccountErasureReport(base_cvs=3, files_unlinked=3, unlink_failures=())
    assert order == [
        "files_of_account",
        "delete_account",
        "file_deleted",
        "file_deleted",
        "file_deleted",
    ]
    assert files.data == {}


async def test_erasing_an_unknown_user_raises_account_not_found() -> None:
    accounts = FakeAccountDataPort({})  # empty: no such account
    files = InMemoryFileStore()
    use_case = EraseAccount(accounts, files)

    with pytest.raises(AccountNotFound):
        await use_case(UserId(value=uuid4()))

    assert files.delete_calls == []


async def test_a_second_call_for_the_same_user_raises_account_not_found_and_unlinks_nothing() -> (
    None
):
    """AC-11: the ordinary "erase, then erase again" case."""
    user_id = UserId(value=uuid4())
    refs = [_numbered_ref(1)]
    accounts = FakeAccountDataPort({user_id: refs})
    files = InMemoryFileStore()
    for ref in refs:
        await files.put(ref, b"bytes")
    use_case = EraseAccount(accounts, files)

    first = await use_case(user_id)
    assert first.files_unlinked == 1

    with pytest.raises(AccountNotFound):
        await use_case(user_id)

    # the second, losing call touched no more files than the first already did
    assert len(files.delete_calls) == 1


async def test_delete_account_losing_the_race_raises_account_not_found_and_unlinks_nothing() -> (
    None
):
    """S-46's narrower shape: `files_of_account` reads a stale-but-true snapshot, and a concurrent
    erasure's `delete_account` wins first — `delete_account` returns `False` even though the keys
    were genuinely read. Nothing is unlinked; the loser's read is thrown away."""
    user_id = UserId(value=uuid4())
    refs = [_numbered_ref(1), _numbered_ref(2)]
    accounts = FakeAccountDataPort({user_id: refs})
    accounts.force_next_delete_account_result(False)
    files = InMemoryFileStore()
    for ref in refs:
        await files.put(ref, b"bytes")
    use_case = EraseAccount(accounts, files)

    with pytest.raises(AccountNotFound):
        await use_case(user_id)

    assert files.delete_calls == []
    assert files.data != {}  # nothing was touched


async def test_some_unlinks_failing_are_returned_never_logged(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-11 / S-45: `unlink_failures` names the failing key's exception **type**, the report is
    returned rather than raised, and `application/` never logs it itself."""
    configure_logging(settings)
    user_id = UserId(value=uuid4())
    ok_ref, failing_ref = _numbered_ref(1), _numbered_ref(2)
    accounts = FakeAccountDataPort({user_id: [ok_ref, failing_ref]})
    files = InMemoryFileStore(
        fail_delete=FileStoreUnavailable("simulated EIO — must never reach a log line"),
        fail_delete_keys={failing_ref.key},
    )
    await files.put(ok_ref, b"bytes")
    await files.put(failing_ref, b"bytes")
    use_case = EraseAccount(accounts, files)

    with caplog.at_level("DEBUG"):
        report = await use_case(user_id)

    assert report.base_cvs == 2
    assert report.files_unlinked == 1
    assert report.unlink_failures == ("FileStoreUnavailable",)
    # the discriminating positive above (a real failure, named by type) makes this absence honest:
    assert caplog.records == []
