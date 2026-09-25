"""Application tests for `ListSavedBaseCvs` (T9, RED, slice 2.2, AC-8).

The user-side sibling of `ListBaseCvsForSession` (1.1): "every saved base CV this user owns, newest
first, or an empty sequence — never a 404." Authorized **by construction** — `list_for_user` queries
by exactly the resolved user's id — so this file's job is to prove the use case resolves the user
first (AC-8's "not `UserNotFound`" defense-in-depth every user use case shares) and never returns a
row it does not own, rather than to re-prove `FakeBaseCvRepository.list_for_user`'s own filter (that
belongs to `test_base_cv_repository.py`, once T13 gives it a real adapter to test).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tailorcraft.application.intake.list_saved_base_cvs import ListSavedBaseCvs
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
from tailorcraft.domain.intake.value_objects import CvContentType, OriginalFilename
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import FakeBaseCvRepository, FakeUserRepository

_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZWhhc2g")


async def _seed_user(users: FakeUserRepository, *, email: str = "alex@example.com") -> User:
    user = User.register_with_password(
        users.next_identity(),
        EmailAddress.parse(email),
        _HASH,
        at=datetime(2026, 9, 4, 0, 0, 0, tzinfo=UTC),
    )
    user.release_events()
    await users.add(user)
    return user


def _saved_cv(cvs: FakeBaseCvRepository, owner: UserOwner, *, uploaded_at: datetime) -> BaseCv:
    cv_id = cvs.next_identity()
    return BaseCv.upload(
        id=cv_id,
        owner=owner,
        original_filename=OriginalFilename("cv.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=10,
        file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
        uploaded_at=uploaded_at,
    )


async def test_a_user_with_no_saved_cvs_gets_an_empty_sequence_not_a_404() -> None:
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()
    use_case = ListSavedBaseCvs(cvs, users)

    result = await use_case(user.id)

    assert list(result) == []


async def test_a_gone_user_raises_user_not_found() -> None:
    users = FakeUserRepository()  # empty: no such user
    cvs = FakeBaseCvRepository()
    use_case = ListSavedBaseCvs(cvs, users)

    with pytest.raises(UserNotFound):
        await use_case(UserId(value=uuid4()))


async def test_returns_only_this_users_saved_cvs_newest_first(clock: FixedClock) -> None:
    """Another user's saved CV and a guest's workspace CV both exist; neither is in the answer, and
    this user's own three come back newest-uploaded-first."""
    users = FakeUserRepository()
    user = await _seed_user(users, email="alex@example.com")
    other_user = await _seed_user(users, email="other@example.com")
    cvs = FakeBaseCvRepository()

    oldest = _saved_cv(cvs, UserOwner(user.id), uploaded_at=clock.now())
    middle = _saved_cv(cvs, UserOwner(user.id), uploaded_at=clock.now().replace(hour=13))
    newest = _saved_cv(cvs, UserOwner(user.id), uploaded_at=clock.now().replace(hour=14))
    await cvs.add(oldest)
    await cvs.add(middle)
    await cvs.add(newest)

    # a decoy from another user and a decoy from a guest session — neither must appear
    await cvs.add(_saved_cv(cvs, UserOwner(other_user.id), uploaded_at=clock.now()))
    guest_decoy_id = cvs.next_identity()
    guest_decoy = BaseCv.upload(
        id=guest_decoy_id,
        owner=GuestOwner(GuestSessionId(value=uuid4())),
        original_filename=OriginalFilename("guest.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=10,
        file=FileRef.for_base_cv(guest_decoy_id, CvContentType.PDF),
        uploaded_at=clock.now(),
    )
    await cvs.add(guest_decoy)

    use_case = ListSavedBaseCvs(cvs, users)
    result = await use_case(user.id)

    assert [cv.id for cv in result] == [newest.id, middle.id, oldest.id]
    assert guest_decoy.id not in [cv.id for cv in result]
