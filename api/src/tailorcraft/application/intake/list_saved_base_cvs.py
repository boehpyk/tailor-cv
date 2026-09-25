"""The `ListSavedBaseCvs` use case: every base CV a registered user keeps (slice 2.2, AC-8).

The user-side sibling of `ListBaseCvsForSession`, and authorized the same way — **by construction**:
`list_for_user` queries by exactly the resolved user's id, so there is no row in the answer the
caller does not own, and nothing the caller supplies other than its own verified id parameterizes
the query.

**No command dataclass**, like the guest reads it mirrors: the only input is the verified `UserId`,
and a one-field wrapper would be a contract with nothing to say.

**SKELETON step (T8).** `__init__` is real and stores its ports, so `qa`'s T9 tests fail on their
assertions rather than on a `TypeError`; `__call__`'s body lands in T10.
"""

from __future__ import annotations

from collections.abc import Sequence

from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.ports import BaseCvRepository


class ListSavedBaseCvs:
    """Every saved `BaseCv` `user_id` owns, newest first, or an empty sequence — never a 404.

    Flow (technical plan §2): ``resolve_existing_user`` (→ `UserNotFound`) → ``list_for_user``.
    """

    def __init__(self, cvs: BaseCvRepository, users: UserRepository) -> None:
        self._cvs = cvs
        self._users = users

    async def __call__(self, user_id: UserId) -> Sequence[BaseCv]:
        raise NotImplementedError
