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
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.application.identity.resolve_existing_user import resolve_existing_user
from tailorcraft.application.intake.owned_saved_base_cv import get_owned_saved_base_cv
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.ports import BaseCvRepository
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.shared.files import FileRef, FileStorePort


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
        user = await resolve_existing_user(self._users, user_id)
        cv = await get_owned_saved_base_cv(self._cvs, base_cv_id, user.id)

        # Both read into locals *before* `remove`: once the row is gone (and, in the real adapter,
        # the transaction committed) the aggregate is the only thing left that knows its key, and a
        # still-attached instance is expired by a commit — 1.4's "read ids before the flush".
        cv_id, ref = cv.id, cv.file
        cv.delete(self._clock.now())

        # Rows first (ADR-0006 §2). Zero rows → `BaseCvNotFound` propagates from here, and the file
        # below is never touched: a concurrent delete that won owns that unlink, not this loser.
        await self._cvs.remove(cv_id, UserOwner(user.id))

        result = await self._unlink(ref)

        # The row-level fact happened whatever the file's fate, so the event is published either
        # way — after the remove, never before.
        await self._events.publish(*cv.release_events())
        return result

    async def _unlink(self, ref: FileRef) -> DeleteSavedBaseCvResult:
        """`files.delete`, never `delete_partial` — a row's key is always a final key.

        `Exception`, never `BaseException`: a cancellation must still cancel. Only the class name
        leaves this method; the message can quote a path, and this layer does not log (the entry
        point writes the one warning line).
        """
        try:
            await self._files.delete(ref)
        except Exception as exc:
            return DeleteSavedBaseCvResult(
                file_unlinked=False, unlink_error_type=type(exc).__name__
            )
        return DeleteSavedBaseCvResult(file_unlinked=True, unlink_error_type=None)
