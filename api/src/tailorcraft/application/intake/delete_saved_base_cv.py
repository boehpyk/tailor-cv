"""The `DeleteSavedBaseCv` use case: remove one saved base CV — the row, then its file (slice 2.2,
technical plan §0.4, AC-10).

**Rows first, committed, then files** (ADR-0006 §2): the survivor of a crash between the two is an
orphan file the sweep reclaims, never a row that answers 410. This layer cannot name a transaction
(ADR-0002), so the durability half of that sentence is the bound `BaseCvRepository`'s: the delete
route's composition root wraps the repository in a committing adapter whose `remove` commits.

**A failed unlink does not raise.** By then the CV is gone from everything the user can reach, and a
5xx would be a lie in the other direction — a retry would 404. The result carries `file_unlinked` and
the exception's **type name**, never its message (a message can quote a path); the entry point turns
that into one warning line. This layer does not log.

**No command dataclass**, like the other saved-CV use cases: two verified ids.

**SKELETON step (T8).** `__init__` is real; `__call__`'s body lands in T10.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.ports import BaseCvRepository
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.shared.files import FileStorePort


@dataclass(frozen=True, slots=True)
class DeleteSavedBaseCvResult:
    """What happened to the file after the row was durably removed.

    `unlink_error_type` is set iff `file_unlinked` is `False`: the exception's class name, the one
    fact an operator needs, and a string with no room for a key, a path or a message.
    """

    file_unlinked: bool
    unlink_error_type: str | None


class DeleteSavedBaseCv:
    """Delete one of `user_id`'s saved base CVs.

    Flow (technical plan §0.4): ``resolve_existing_user`` (→ `UserNotFound`) → ``cvs.get`` (→
    `BaseCvNotFound`) → owner check (→ `BaseCvNotFound` from `BaseCvNotOwnedByUser`) →
    ``ref = cv.file`` **into a local** → ``cv.delete(clock.now())`` records `BaseCvDeleted` →
    ``cvs.remove(cv.id, UserOwner(user_id))`` (zero rows → `BaseCvNotFound`, **nothing unlinked**) →
    ``files.delete(ref)`` — never `delete_partial`: a row's key is always a final key — with any
    `Exception` from it caught into the result → publish the released events.
    """

    def __init__(
        self,
        cvs: BaseCvRepository,
        users: UserRepository,
        files: FileStorePort,
        events: EventPublisherPort,
        clock: Clock,
    ) -> None:
        self._cvs = cvs
        self._users = users
        self._files = files
        self._events = events
        self._clock = clock

    async def __call__(self, base_cv_id: BaseCvId, user_id: UserId) -> DeleteSavedBaseCvResult:
        raise NotImplementedError
