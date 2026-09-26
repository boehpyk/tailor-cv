"""The `CopySavedBaseCvToWorkspace` use case: put a working copy of a saved base CV into the guest
workspace (slice 2.2, technical plan §0.2, AC-9).

**Reuse is a copy.** An ownership graph never crosses owners: every row a run, an export or the
purge can reach has the run's owner, so the saved CV itself never enters the workspace. The copy is a
new `BaseCv` with a new id, **its own file** (the purge that deletes the copy's session unlinks the
copy's bytes, never the source's — AC-17), and the source's extracted text.

**No extractor, anywhere — not even in the constructor.** The source was extracted once; the copy
carries that text (I-8). A constructor that took a `CvTextExtractorPort` it promised not to call
would be a promise; one that cannot receive it is a fact (AC-9's strongest form).

**Each credential authorizes only its own half** (ADR-0008 amendment (f)): `user_id` authorizes
reading the source, `guest_session_id` owns the destination. Neither is asked "is there a user or a
guest?" — the copy needs both and uses each for one thing.

**Not idempotent, on purpose** (S-36): two clicks, two working copies, both guest-capped and both
gone within 24 h.

**No command dataclass**, like the other saved-CV use cases: three verified ids.
"""

from __future__ import annotations

from tailorcraft.application.identity.resolve_existing_user import resolve_existing_user
from tailorcraft.application.identity.resolve_guest_session import resolve_active_guest_session
from tailorcraft.application.intake.owned_saved_base_cv import get_owned_saved_base_cv
from tailorcraft.application.intake.upload_base_cv import UploadBaseCvResult
from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.identity.ports import GuestSessionRepository, UserRepository
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import (
    SavedBaseCvFileMissing,
    SavedBaseCvNotCopyable,
    TooManyBaseCvs,
)
from tailorcraft.domain.intake.ports import BaseCvRepository
from tailorcraft.domain.intake.value_objects import BaseCvId, BaseCvStatus
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.shared.files import FileRef, FileStorePort, StoredFileMissing


class CopySavedBaseCvToWorkspace:
    """Copy `saved_base_cv_id` (owned by `user_id`) into `guest_session_id`'s workspace.

    Returns `UploadBaseCvResult` — the same type an upload returns, not a look-alike: the copy is
    answered on the wire exactly as an upload is (a `BaseCvResponse` re-read from the repository), so
    a second result type with the same four fields would be a distinction without a difference.

    Flow (technical plan §2): ``resolve_existing_user`` (→ `UserNotFound`) → ``cvs.get(source)`` (→
    `BaseCvNotFound`) → owner check against `UserOwner(user_id)` (→ `BaseCvNotFound` from
    `BaseCvNotOwnedByUser`) → source `EXTRACTED` (else `SavedBaseCvNotCopyable`) →
    ``resolve_active_guest_session`` (→ `GuestSessionNotFound` / `GuestSessionExpired`) → the
    **guest** cap (`TooManyBaseCvs`) → ``next_identity`` → ``files.get(source.file)`` — on
    `StoredFileMissing`, re-``get`` the source row: gone → `BaseCvNotFound`, present →
    `SavedBaseCvFileMissing` → ``files.put(new_ref, data)`` **before** the row →
    ``BaseCv.copy_from`` → ``cvs.add`` → publish `BaseCvCopied`.
    """

    def __init__(
        self,
        cvs: BaseCvRepository,
        users: UserRepository,
        sessions: GuestSessionRepository,
        files: FileStorePort,
        events: EventPublisherPort,
        clock: Clock,
        max_per_session: int = 5,
    ) -> None:
        self._cvs = cvs
        self._users = users
        self._sessions = sessions
        self._files = files
        self._events = events
        self._clock = clock
        self._max_per_session = max_per_session

    async def __call__(
        self,
        saved_base_cv_id: BaseCvId,
        user_id: UserId,
        guest_session_id: GuestSessionId,
    ) -> UploadBaseCvResult:
        # The bearer's half first: the source is authorized before the guest session is even read.
        user = await resolve_existing_user(self._users, user_id)
        source = await get_owned_saved_base_cv(self._cvs, saved_base_cv_id, user.id)
        if source.status is not BaseCvStatus.EXTRACTED:
            # Checked here rather than left to `copy_from`'s `InvariantViolated`: this is an
            # ordinary state of a saved CV the caller must be told about, not a programming error.
            raise SavedBaseCvNotCopyable(source.status)

        # The cookie's half: it owns the destination and authorizes nothing else.
        session = await resolve_active_guest_session(self._sessions, self._clock, guest_session_id)
        # The destination's cap — the guest one, `UploadBaseCv`'s cross-aggregate policy (F-23),
        # since the copy is a workspace CV like any other (S-30).
        if await self._cvs.count_for_session(session.id) >= self._max_per_session:
            raise TooManyBaseCvs(str(session.id))

        data = await self._read_source_bytes(source)

        copy_id = self._cvs.next_identity()
        ref = FileRef.for_base_cv(copy_id, source.content_type)
        # File before row, as in `UploadBaseCv` (ADR-0006 §2): a crash here leaves an orphan file
        # for the sweep, never a row pointing at nothing.
        await self._files.put(ref, data)

        copy = BaseCv.copy_from(
            source, id=copy_id, into=GuestOwner(session.id), file=ref, at=self._clock.now()
        )
        await self._cvs.add(copy)
        await self._events.publish(*copy.release_events())

        text = copy.extracted_text
        return UploadBaseCvResult(
            base_cv_id=copy.id,
            status=copy.status,
            character_count=text.character_count if text else None,
            failure_reason=copy.failure_reason,
        )

    async def _read_source_bytes(self, source: BaseCv) -> bytes:
        """The source's bytes, or the reason there are none.

        `StoredFileMissing` means one of two very different things, and only the row can say which:
        the source was deleted since it was loaded (a concurrent delete, S-33 — `BaseCvNotFound`
        from the re-read propagates), or the row is still there and its file is not (a bug
        somewhere, S-32 — `SavedBaseCvFileMissing`, not a 404 claiming the CV never existed).
        """
        try:
            return await self._files.get(source.file)
        except StoredFileMissing:
            await self._cvs.get(source.id)
            raise SavedBaseCvFileMissing(str(source.id)) from None
