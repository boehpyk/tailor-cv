"""The `UploadBaseCv` use case: accept an uploaded base CV, store it, and attempt extraction.

This is the first use case in the codebase, so its shape is the one every later slice copies:
a frozen input dataclass as the contract, every dependency arriving through the constructor as a
`Protocol` port, and a `__call__` that is the only public behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.identity.ports import GuestSessionRepository
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.ports import BaseCvRepository, CvTextExtractorPort
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    BaseCvStatus,
    CvContentType,
    ExtractionFailureReason,
    OriginalFilename,
)
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.shared.files import FileStorePort


@dataclass(frozen=True, slots=True)
class UploadBaseCvCommand:
    """What the caller supplies. `content_type` has already been sniffed at the boundary
    (`sniff_cv_content_type`) — this use case never inspects `content` to decide its type, and
    never trusts a client-supplied `Content-Type` header or filename extension."""

    guest_session_id: GuestSessionId
    original_filename: OriginalFilename
    content_type: CvContentType
    content: bytes


@dataclass(frozen=True, slots=True)
class UploadBaseCvResult:
    """What the caller gets back. `character_count` is set iff `status == EXTRACTED`;
    `failure_reason` is set iff `status == EXTRACTION_FAILED` — the same I-2 shape the aggregate
    itself enforces, mirrored here rather than handing the caller the aggregate."""

    base_cv_id: BaseCvId
    status: BaseCvStatus
    character_count: int | None
    failure_reason: ExtractionFailureReason | None


class UploadBaseCv:
    """Accept an uploaded base CV for a guest session: store the bytes, create the aggregate, and
    attempt extraction — recording success or failure as a state rather than letting either escape
    as an exception (ADR-0004).

    Flow (technical-plan.md "Application layer"):

    1. ``session = await sessions.get(cmd.guest_session_id)`` — raises `GuestSessionNotFound` if the
       session row is gone.
    2. ``if session.is_expired(clock.now()): raise GuestSessionExpired``.
    3. ``if await cvs.count_for_session(session.id) >= max_per_session: raise TooManyBaseCvs`` —
       a **cross-aggregate policy**, deliberately not an invariant of `BaseCv`: the rule spans every
       `BaseCv` a session owns, a fact no single `BaseCv` instance has access to, so it lives here
       with this comment rather than on the aggregate (F-23, OQ-10).
    4. ``cv_id = cvs.next_identity()``; ``ref = FileRef.for_base_cv(cv_id, cmd.content_type)``.
    5. ``await files.put(ref, cmd.content)`` — **before** the row exists. `OSError` is already
       translated to `FileStoreUnavailable` by the adapter. Writing the file first is the deliberate
       crash-window choice from ADR-0006 §2: the survivor of a crash here is an orphan *file*, which
       a directory sweep can reclaim, rather than a row pointing at nothing.
    6. ``cv = BaseCv.upload(...)`` — records `BaseCvUploaded`.
    7. ``try: text = await extractor.extract(cmd.content_type, cmd.content)`` then
       ``cv.mark_extracted(text, clock.now())``; ``except CvExtractionFailed as exc:`` then
       ``cv.mark_extraction_failed(exc.reason, clock.now())``. **The failure does not propagate** —
       it becomes a state, exactly as ADR-0004 requires of a failed `TailoringRun`.
    8. ``await cvs.add(cv)``.
    9. ``await events.publish(*cv.release_events())`` — released and dispatched **after** the
       aggregate is saved, never before.
    10. Return `UploadBaseCvResult` built from the saved aggregate.
    """

    def __init__(
        self,
        cvs: BaseCvRepository,
        sessions: GuestSessionRepository,
        files: FileStorePort,
        extractor: CvTextExtractorPort,
        events: EventPublisherPort,
        clock: Clock,
        max_per_session: int = 5,
    ) -> None:
        self._cvs = cvs
        self._sessions = sessions
        self._files = files
        self._extractor = extractor
        self._events = events
        self._clock = clock
        self._max_per_session = max_per_session

    async def __call__(self, cmd: UploadBaseCvCommand) -> UploadBaseCvResult:
        raise NotImplementedError
