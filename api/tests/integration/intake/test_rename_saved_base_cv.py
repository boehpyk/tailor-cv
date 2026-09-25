"""Application tests for `RenameSavedBaseCv` (T9, RED, slice 2.2, AC-4, AC-8, S-16).

"Authorization is one equality" (AC-8) — this file's authorization tests are the first ones written
against that rule at use-case level: a guest-owned id, another user's id and a nonexistent id must
all raise the same public `BaseCvNotFound`, distinguishable only on `__cause__` for this file's own
assertions. `BaseCvLabel`'s own validation (AC-4, empty/too-long/control-character refusals) is
already covered where it belongs — `tests/unit/intake/test_value_objects.py` — so this file only
ever constructs *valid* labels; the use case receives an already-validated `BaseCvLabel | None` and
has nothing left to validate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tailorcraft.application.intake.rename_saved_base_cv import RenameSavedBaseCv
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
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    BaseCvLabel,
    CvContentType,
    OriginalFilename,
)
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import FakeBaseCvRepository, FakeUserRepository

_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZWhhc2g")


class _DeletingBaseCvRepository(FakeBaseCvRepository):
    """Removes the row the instant `get` returns it — models S-16 ("rename races a delete") without
    real concurrency: the use case's own `get` already handed back a `BaseCv`, and by the time it
    calls `save_label` the row is gone, exactly as a concurrent `DELETE` winning the race would leave
    it."""

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
    return BaseCv.upload(
        id=cv_id,
        owner=owner,
        original_filename=OriginalFilename("cv.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=10,
        file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
        uploaded_at=at,
    )


async def test_setting_a_label_on_the_owners_own_cv_persists_it(clock: FixedClock) -> None:
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()
    cv = _saved_cv(cvs, UserOwner(user.id), clock.now())
    await cvs.add(cv)
    use_case = RenameSavedBaseCv(cvs, users, clock)

    result = await use_case(cv.id, user.id, BaseCvLabel("Backend roles"))

    assert result.label == BaseCvLabel("Backend roles")
    persisted = await cvs.get(cv.id)
    assert persisted.label == BaseCvLabel("Backend roles")


async def test_clearing_the_label_with_none_persists_the_clear(clock: FixedClock) -> None:
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()
    cv = _saved_cv(cvs, UserOwner(user.id), clock.now())
    cv.rename(BaseCvLabel("old label"), clock.now())
    await cvs.add(cv)
    use_case = RenameSavedBaseCv(cvs, users, clock)

    result = await use_case(cv.id, user.id, None)

    assert result.label is None
    persisted = await cvs.get(cv.id)
    assert persisted.label is None


async def test_a_gone_user_raises_user_not_found(clock: FixedClock) -> None:
    users = FakeUserRepository()  # empty: no such user
    cvs = FakeBaseCvRepository()
    use_case = RenameSavedBaseCv(cvs, users, clock)

    with pytest.raises(UserNotFound):
        await use_case(BaseCvId(value=uuid4()), UserId(value=uuid4()), BaseCvLabel("x"))


async def test_a_nonexistent_cv_id_raises_base_cv_not_found(clock: FixedClock) -> None:
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()  # empty
    use_case = RenameSavedBaseCv(cvs, users, clock)

    with pytest.raises(BaseCvNotFound):
        await use_case(BaseCvId(value=uuid4()), user.id, BaseCvLabel("x"))


async def test_a_guest_owned_cv_id_raises_base_cv_not_found_chained_from_not_owned_by_user(
    clock: FixedClock,
) -> None:
    """AC-8: "a user-owned id on a guest route is `BaseCvNotFound`" has its mirror here — a
    guest-owned id on a *user* route is the same public `BaseCvNotFound`, distinguishable only on
    `__cause__`."""
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
    use_case = RenameSavedBaseCv(cvs, users, clock)

    with pytest.raises(BaseCvNotFound) as exc_info:
        await use_case(guest_cv.id, user.id, BaseCvLabel("x"))

    assert isinstance(exc_info.value.__cause__, BaseCvNotOwnedByUser)


async def test_another_users_cv_id_raises_base_cv_not_found_chained_from_not_owned_by_user(
    clock: FixedClock,
) -> None:
    users = FakeUserRepository()
    owner = await _seed_user(users, email="owner@example.com")
    attacker = await _seed_user(users, email="attacker@example.com")
    cvs = FakeBaseCvRepository()
    cv = _saved_cv(cvs, UserOwner(owner.id), clock.now())
    await cvs.add(cv)
    use_case = RenameSavedBaseCv(cvs, users, clock)

    with pytest.raises(BaseCvNotFound) as exc_info:
        await use_case(cv.id, attacker.id, BaseCvLabel("x"))

    assert isinstance(exc_info.value.__cause__, BaseCvNotOwnedByUser)
    # the target's own row is untouched
    assert (await cvs.get(cv.id)).label is None


async def test_a_rename_racing_a_concurrent_delete_raises_base_cv_not_found(
    clock: FixedClock,
) -> None:
    """S-16: the row is gone by the time `save_label` runs — `BaseCvNotFound`, nothing written."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = _DeletingBaseCvRepository()
    cv = _saved_cv(cvs, UserOwner(user.id), clock.now())
    await cvs.add(cv)
    use_case = RenameSavedBaseCv(cvs, users, clock)

    with pytest.raises(BaseCvNotFound):
        await use_case(cv.id, user.id, BaseCvLabel("x"))
