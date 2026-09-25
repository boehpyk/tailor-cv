"""The `RenameSavedBaseCv` use case: set or clear a saved base CV's label (slice 2.2, OQ-5, AC-8).

The smallest mutation that exercises the owner check on an update. Authorization is one equality,
exactly 1.1's shape on the other owner: `cv.owner == UserOwner(user_id)`, and "not mine" raises
`BaseCvNotFound` chained from `BaseCvNotOwnedByUser` — indistinguishable from "does not exist" at
the boundary, distinguishable on `__cause__` for this use case's own tests.

`label=None` clears the label; the value object already refused anything that cannot be a display
name before this use case was called (`InvalidLabel`, AC-4).

**No command dataclass**, like `GetBaseCvForSession`: three already-validated values, each a domain
type, with nothing to validate between them.

**SKELETON step (T8).** `__init__` is real; `__call__`'s body lands in T10.
"""

from __future__ import annotations

from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.ports import BaseCvRepository
from tailorcraft.domain.intake.value_objects import BaseCvId, BaseCvLabel
from tailorcraft.domain.shared.clock import Clock


class RenameSavedBaseCv:
    """Rename one of `user_id`'s saved base CVs and return it as persisted.

    Flow (technical plan §2): ``resolve_existing_user`` (→ `UserNotFound`) → ``cvs.get`` (→
    `BaseCvNotFound`) → owner check (→ `BaseCvNotFound` from `BaseCvNotOwnedByUser`) →
    ``cv.rename(label, clock.now())`` → ``cvs.save_label(cv)`` (→ `BaseCvNotFound` if a concurrent
    delete won). No event: a label is not a fact anything listens for.
    """

    def __init__(self, cvs: BaseCvRepository, users: UserRepository, clock: Clock) -> None:
        self._cvs = cvs
        self._users = users
        self._clock = clock

    async def __call__(
        self, base_cv_id: BaseCvId, user_id: UserId, label: BaseCvLabel | None
    ) -> BaseCv:
        raise NotImplementedError
