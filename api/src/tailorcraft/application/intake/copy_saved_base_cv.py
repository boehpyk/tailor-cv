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

**SKELETON step (T8).** `__init__` is real; `__call__`'s body lands in T10.
"""

from __future__ import annotations

from tailorcraft.application.intake.upload_base_cv import UploadBaseCvResult
from tailorcraft.domain.identity.ports import GuestSessionRepository, UserRepository
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.intake.ports import BaseCvRepository
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.shared.files import FileStorePort


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
        raise NotImplementedError
