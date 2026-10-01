"""`get_owned_saved_base_cv`: load a base CV and refuse it unless `user_id` owns it (slice 2.2, AC-8).

`RenameSavedBaseCv` and `DeleteSavedBaseCv` both start with the same two steps — ``cvs.get`` then
one equality — and "authorization is one equality" is exactly the rule that must not drift between
two copies of it. (2.2's `CopySavedBaseCvToWorkspace` was the third caller until slice 2.4 removed
it, ADR-0022 amendment (d).) A plain function for `resolve_active_guest_session`'s
reason: it holds no state, and every dependency it needs already sits on the caller.

**Not yours is not there.** A guest-owned id, another user's id and a nonexistent id all raise the
same public `BaseCvNotFound`; the reason survives only on `__cause__` (`BaseCvNotOwnedByUser`), for
this layer's own tests — the boundary answers 404 for all three and never a 403 (ADR-0008).
"""

from __future__ import annotations

from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound, BaseCvNotOwnedByUser
from tailorcraft.domain.intake.ports import BaseCvRepository
from tailorcraft.domain.intake.value_objects import BaseCvId


async def get_owned_saved_base_cv(
    cvs: BaseCvRepository, base_cv_id: BaseCvId, user_id: UserId
) -> BaseCv:
    """Return the `BaseCv` with `base_cv_id` if its owner is `UserOwner(user_id)`.

    Raises `BaseCvNotFound` if there is no such row, and `BaseCvNotFound` chained from
    `BaseCvNotOwnedByUser` if there is one and it belongs to anyone else — a guest included.
    """
    cv = await cvs.get(base_cv_id)
    if cv.owner != UserOwner(user_id):
        raise BaseCvNotFound(str(base_cv_id)) from BaseCvNotOwnedByUser(str(base_cv_id))
    return cv
