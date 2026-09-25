"""Application tests for `DeleteSavedBaseCv` (T9, RED, slice 2.2, AC-8, AC-10, S-19, S-24).

**Order is the whole point of this use case** (technical plan §0.4): the row is removed and durable
before the file is ever touched, so the survivor of a crash between the two is an orphan file the
sweep reclaims, never a row pointing at nothing. `_OrderRecordingBaseCvRepository` and
`_OrderRecordingFileStore` below append to one shared list — the "recording double" the task list
asks for — so the order assertion is a plain list equality rather than an inference from end state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tailorcraft.application.intake.delete_saved_base_cv import (
    DeleteSavedBaseCv,
    DeleteSavedBaseCvResult,
)
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    GuestSessionId,
    PasswordHash,
    UserId,
)
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound, BaseCvNotOwnedByUser
from tailorcraft.domain.intake.events import BaseCvDeleted
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType, OriginalFilename
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeBaseCvRepository,
    FakeUserRepository,
    InMemoryFileStore,
    RecordingEventPublisher,
)

_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZWhhc2g")


class _OrderRecordingBaseCvRepository(FakeBaseCvRepository):
    """Appends `"row_removed"` to a shared list the instant `remove` actually removes a row —
    never on the refusal path, so a test asserting the row was never reached can tell that apart
    from "reached and refused"."""

    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    async def remove(self, cv_id: BaseCvId, owner: UserOwner) -> None:
        await super().remove(cv_id, owner)
        self._order.append("row_removed")


class _OrderRecordingFileStore(InMemoryFileStore):
    """Appends `"file_deleted"` to the same shared list the instant `delete` is called — before the
    (possibly failing) unlink itself, so the order is recorded even when the unlink then raises."""

    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    async def delete(self, ref: FileRef) -> None:
        self._order.append("file_deleted")
        await super().delete(ref)


class _DeletingBaseCvRepository(FakeBaseCvRepository):
    """Removes the row the instant `get` returns it — models S-19's concurrent delete the same way
    `test_rename_saved_base_cv.py`'s twin does for S-16."""

    async def get(self, cv_id: BaseCvId) -> BaseCv:
        cv = await super().get(cv_id)
        del self._by_id[cv_id]
        return cv


async def _seed_user(users: FakeUserRepository, *, email: str = "alex@example.com") -> User:
    user = User.register_with_password(
        users.next_identity(), EmailAddress.parse(email), _HASH, at=datetime(2026, 9, 4, tzinfo=UTC)
    )
    user.release_events()
    await users.add(user)
    return user


def _saved_cv(cvs: FakeBaseCvRepository, owner: UserOwner, at: datetime) -> BaseCv:
    cv_id = cvs.next_identity()
    cv = BaseCv.upload(
        id=cv_id,
        owner=owner,
        original_filename=OriginalFilename("cv.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=10,
        file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
        uploaded_at=at,
    )
    cv.release_events()
    return cv


async def test_happy_path_removes_the_row_then_unlinks_the_file_in_that_order(
    clock: FixedClock,
) -> None:
    users = FakeUserRepository()
    user = await _seed_user(users)
    order: list[str] = []
    cvs = _OrderRecordingBaseCvRepository(order)
    cv = _saved_cv(cvs, UserOwner(user.id), clock.now())
    await cvs.add(cv)
    files = _OrderRecordingFileStore(order)
    await files.put(cv.file, b"pdf bytes")
    events = RecordingEventPublisher()
    use_case = DeleteSavedBaseCv(cvs, users, files, events, clock)

    result = await use_case(cv.id, user.id)

    assert result == DeleteSavedBaseCvResult(file_unlinked=True, unlink_error_type=None)
    assert order == ["row_removed", "file_deleted"]
    assert files.delete_partial_calls == []
    with pytest.raises(BaseCvNotFound):
        await cvs.get(cv.id)
    assert files.data == {}

    event_types = [type(event) for event in events.published]
    assert event_types == [BaseCvDeleted]


async def test_a_gone_user_raises_user_not_found(clock: FixedClock) -> None:
    users = FakeUserRepository()  # empty: no such user
    cvs = FakeBaseCvRepository()
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = DeleteSavedBaseCv(cvs, users, files, events, clock)

    with pytest.raises(UserNotFound):
        await use_case(BaseCvId(value=uuid4()), UserId(value=uuid4()))

    assert files.delete_calls == []


async def test_a_nonexistent_cv_id_raises_base_cv_not_found(clock: FixedClock) -> None:
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()  # empty
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = DeleteSavedBaseCv(cvs, users, files, events, clock)

    with pytest.raises(BaseCvNotFound):
        await use_case(BaseCvId(value=uuid4()), user.id)

    assert files.delete_calls == []


async def test_a_guest_owned_cv_id_raises_base_cv_not_found_chained_from_not_owned_by_user(
    clock: FixedClock,
) -> None:
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()
    guest_cv_id = cvs.next_identity()
    guest_cv = BaseCv.upload(
        id=guest_cv_id,
        owner=GuestOwner(GuestSessionId(value=uuid4())),
        original_filename=OriginalFilename("guest.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=10,
        file=FileRef.for_base_cv(guest_cv_id, CvContentType.PDF),
        uploaded_at=clock.now(),
    )
    await cvs.add(guest_cv)
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = DeleteSavedBaseCv(cvs, users, files, events, clock)

    with pytest.raises(BaseCvNotFound) as exc_info:
        await use_case(guest_cv.id, user.id)

    assert isinstance(exc_info.value.__cause__, BaseCvNotOwnedByUser)
    assert files.delete_calls == []
    # the guest row is untouched
    assert (await cvs.get(guest_cv.id)) is guest_cv


async def test_another_users_cv_id_raises_base_cv_not_found_chained_from_not_owned_by_user(
    clock: FixedClock,
) -> None:
    users = FakeUserRepository()
    owner = await _seed_user(users, email="owner@example.com")
    attacker = await _seed_user(users, email="attacker@example.com")
    cvs = FakeBaseCvRepository()
    cv = _saved_cv(cvs, UserOwner(owner.id), clock.now())
    await cvs.add(cv)
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = DeleteSavedBaseCv(cvs, users, files, events, clock)

    with pytest.raises(BaseCvNotFound) as exc_info:
        await use_case(cv.id, attacker.id)

    assert isinstance(exc_info.value.__cause__, BaseCvNotOwnedByUser)
    assert files.delete_calls == []
    assert (await cvs.get(cv.id)) is cv


async def test_a_concurrent_delete_that_already_won_raises_base_cv_not_found_and_unlinks_nothing(
    clock: FixedClock,
) -> None:
    """S-19: a second/concurrent `DELETE`. The loser's `remove` finds zero rows and raises
    `BaseCvNotFound`; the file is never touched — the winner's unlink stands alone."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = _DeletingBaseCvRepository()
    cv = _saved_cv(cvs, UserOwner(user.id), clock.now())
    await cvs.add(cv)
    files = InMemoryFileStore()
    await files.put(cv.file, b"pdf bytes")
    events = RecordingEventPublisher()
    use_case = DeleteSavedBaseCv(cvs, users, files, events, clock)

    with pytest.raises(BaseCvNotFound):
        await use_case(cv.id, user.id)

    assert files.delete_calls == []
    # the file the winner would have needed is still there — this loser never touched it
    assert await files.get(cv.file) == b"pdf bytes"


async def test_a_failing_unlink_returns_the_result_unraised_with_the_exception_type_name(
    clock: FixedClock,
) -> None:
    """S-24: by the time the unlink is attempted the row is already gone and committed — a failure
    here does not raise. The result carries `file_unlinked=False` and the exception's type name,
    never its message (a message can quote a path)."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()
    cv = _saved_cv(cvs, UserOwner(user.id), clock.now())
    await cvs.add(cv)
    files = InMemoryFileStore(fail_delete=FileStoreUnavailable("simulated EIO, not in the result"))
    await files.put(cv.file, b"pdf bytes")
    events = RecordingEventPublisher()
    use_case = DeleteSavedBaseCv(cvs, users, files, events, clock)

    result = await use_case(cv.id, user.id)  # must not raise

    assert result.file_unlinked is False
    assert result.unlink_error_type == "FileStoreUnavailable"
    # the row is gone regardless of the unlink's fate
    with pytest.raises(BaseCvNotFound):
        await cvs.get(cv.id)
    # BaseCvDeleted still published — the row-level fact happened, independent of the file's fate
    event_types = [type(event) for event in events.published]
    assert event_types == [BaseCvDeleted]
