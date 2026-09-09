"""The `BaseCv` aggregate: a user's source CV as uploaded, and everything this slice knows about it.

Composes `RecordsEvents` (`domain/shared/events.py`) rather than inheriting a shared aggregate base
class — CLAUDE.md is explicit that two aggregates with the same shape do not get a base class, since
shared shape is not shared behaviour and a base class ends up guessing at rules that differ between
`BaseCv` and the future `TailoringRun`.
"""

from __future__ import annotations

from datetime import datetime

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.errors import ExtractionAlreadyDecided
from tailorcraft.domain.intake.events import (
    BaseCvExtractionFailed,
    BaseCvTextExtracted,
    BaseCvUploaded,
)
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    BaseCvStatus,
    CvContentType,
    ExtractedText,
    ExtractionFailureReason,
    OriginalFilename,
)
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.domain.shared.events import RecordsEvents
from tailorcraft.domain.shared.files import FileRef


# NOT `slots=True`: this aggregate is later mapped by SQLAlchemy's *imperative* mapping
# (`registry.map_imperatively`), which instruments attributes on the instance `__dict__` — a
# `__slots__` class has none, so mapping would fail at wiring time rather than at import time, which
# is a much worse place to discover it. This is a deliberate departure from the value objects a few
# lines up the import list, which are `slots=True` because nothing ever maps them directly: a reader
# who just wrote `slots=True` on five value objects should find the reason here rather than "fix"
# it. The private-attribute names below are exactly what
# `infrastructure/persistence/mapping/intake/base_cv.py` targets, so renaming one here is a breaking
# change to that module too (ADR-0007).
class BaseCv(RecordsEvents):
    """A base CV: the stored bytes (via `FileRef`), the sniffed content type, the original filename,
    and — once decided — the extracted text or the reason extraction failed.

    Invariants (technical-plan.md):

    - **I-1** — A `BaseCv` always has exactly one owner session, one `FileRef` and a
      `size_bytes > 0`. Enforced by `upload()`, which raises `InvariantViolated`; there is no other
      way to construct one.
    - **I-2** — `status == EXTRACTED` iff `extracted_text is not None`; `status ==
      EXTRACTION_FAILED` iff `failure_reason is not None`. Never both. `mark_extracted` and
      `mark_extraction_failed` are the only writers of either.
    - **I-3** — Extraction is decided **once**. A second call to `mark_extracted` or
      `mark_extraction_failed`, from a retry or a race, raises `ExtractionAlreadyDecided` rather than
      silently overwriting the first decision.
    - **I-4** — `extracted_at >= uploaded_at`. Guarded in both `mark_*` methods.
    - **I-5** — `extracted_text`, if present, is a valid `ExtractedText` (≥ 200 non-whitespace
      characters). Enforced by the value object's own `__post_init__`, not here — the aggregate
      cannot hold an invalid one because the type that would carry it cannot exist.

    **Deliberately not invariants of `BaseCv`:** the 10 MB upload size cap is a boundary/config rule
    (`settings.max_upload_bytes`) — the domain must not read settings, so this aggregate has no
    opinion on it beyond "`size_bytes > 0`". The "at most 5 base CVs per session" cap spans every
    `BaseCv` a session owns, a fact no single `BaseCv` instance has access to, so it lives in the
    `UploadBaseCv` use case instead, with a comment there saying why it is not on this class.
    """

    # Class-level annotations only (no assignment): the `__init__` below sets nothing, so this is
    # how mypy --strict learns the attribute types that `upload` sets directly on `self` and the
    # properties below read back. SQLAlchemy's imperative mapping targets these exact names.
    _id: BaseCvId
    _guest_session_id: GuestSessionId
    _original_filename: OriginalFilename
    _content_type: CvContentType
    _size_bytes: int
    _file: FileRef
    _status: BaseCvStatus
    _extracted_text: ExtractedText | None
    _failure_reason: ExtractionFailureReason | None
    _uploaded_at: datetime
    _extracted_at: datetime | None

    def __init__(self) -> None:
        """Takes nothing and does nothing. Build a `BaseCv` with `upload`.

        **An empty constructor looks like something to delete, so here is why it must stay.** This
        class used to define no `__init__` at all and rely on `object.__init__` rejecting keyword
        arguments — that absence was the whole mechanism behind "`upload` is the only constructor".

        The absence stops working the moment the class is mapped, which this one is.
        `registry.map_imperatively` installs a default constructor on a mapped class *that does not
        define one*, and that constructor accepts the **mapped attribute names**, so
        `BaseCv(_size_bytes=5)` was constructible — bypassing `upload` and every rule it enforces.

        Found while building slice 1.2, when the identical hole in `JobPosting` was caught by a test
        the moment its mapping was registered. `domain/posting/job_posting.py::JobPosting.__init__`
        carries the full account, including the two fixes that do **not** work: a raising `__init__`
        breaks the named constructor (a mapped class must be built through `cls()`, because
        SQLAlchemy's instrumentation wrapper is what attaches `_sa_instance_state`), and a private
        sentinel parameter would be persistence leaking into the domain.

        A no-argument `__init__` restores the guarantee exactly and is the smallest thing that does:
        the mapper leaves a user-defined constructor alone, so any argument — public property name
        or private mapped name — is now a `TypeError` from Python's own signature check. The body is
        empty because there is nothing to initialise; `upload` assigns every attribute itself.
        """

    # SQLAlchemy's imperative mapping does not need a constructor at all: it rehydrates a mapped
    # instance through `__new__`, instrumenting `__dict__` directly, and never calls `__init__` on
    # the load path (ADR-0007). The constructor above is for *application* code only.

    @classmethod
    def upload(
        cls,
        id: BaseCvId,
        guest_session_id: GuestSessionId,
        original_filename: OriginalFilename,
        content_type: CvContentType,
        size_bytes: int,
        file: FileRef,
        uploaded_at: datetime,
    ) -> BaseCv:
        """The only constructor. Sets `status = UPLOADED`, `extracted_text = None`,
        `failure_reason = None`, `extracted_at = None`, and records `BaseCvUploaded`.

        Raises `InvariantViolated` if `size_bytes <= 0` (I-1) — the 10 MB *upper* bound is a
        boundary/config concern checked before this is ever called, not by this method.
        """
        if size_bytes <= 0:
            raise InvariantViolated("size_bytes must be > 0 (I-1)")

        cv = cls()
        cv._id = id
        cv._guest_session_id = guest_session_id
        cv._original_filename = original_filename
        cv._content_type = content_type
        cv._size_bytes = size_bytes
        cv._file = file
        cv._status = BaseCvStatus.UPLOADED
        cv._extracted_text = None
        cv._failure_reason = None
        cv._uploaded_at = uploaded_at
        cv._extracted_at = None

        cv.record(
            BaseCvUploaded(
                base_cv_id=id,
                guest_session_id=guest_session_id,
                content_type=content_type,
                size_bytes=size_bytes,
                occurred_at=uploaded_at,
            )
        )
        return cv

    def _guard_extraction_not_yet_decided(self, at: datetime) -> None:
        """The I-3/I-4 guard shared by both `mark_*` methods, written once rather than copy-pasted:
        two copies of an invariant is one copy that gets fixed and one that does not.

        Raises `ExtractionAlreadyDecided` (I-3) if extraction was already decided in either
        direction, and `InvariantViolated` (I-4) if `at < uploaded_at`.
        """
        if self._status is not BaseCvStatus.UPLOADED:
            raise ExtractionAlreadyDecided(
                f"extraction for {self._id!r} was already decided as {self._status!r}"
            )
        if at < self._uploaded_at:
            raise InvariantViolated("extracted_at must be >= uploaded_at (I-4)")

    def mark_extracted(self, text: ExtractedText, at: datetime) -> None:
        """Record that extraction succeeded: sets `status = EXTRACTED`, `extracted_text = text`,
        `extracted_at = at`, and records `BaseCvTextExtracted`.

        Raises `ExtractionAlreadyDecided` (I-3) if extraction was already decided, and
        `InvariantViolated` (I-4) if `at < uploaded_at`.
        """
        self._guard_extraction_not_yet_decided(at)

        self._status = BaseCvStatus.EXTRACTED
        self._extracted_text = text
        self._extracted_at = at

        self.record(
            BaseCvTextExtracted(
                base_cv_id=self._id,
                character_count=text.character_count,
                occurred_at=at,
            )
        )

    def mark_extraction_failed(self, reason: ExtractionFailureReason, at: datetime) -> None:
        """Record that extraction failed: sets `status = EXTRACTION_FAILED`,
        `failure_reason = reason`, `extracted_at = at`, and records `BaseCvExtractionFailed`.

        Raises `ExtractionAlreadyDecided` (I-3) if extraction was already decided, and
        `InvariantViolated` (I-4) if `at < uploaded_at`. This is the ADR-0004 shape: a failed
        extraction is a recorded state of the aggregate, never an exception that escapes to a 500.
        """
        self._guard_extraction_not_yet_decided(at)

        self._status = BaseCvStatus.EXTRACTION_FAILED
        self._failure_reason = reason
        self._extracted_at = at

        self.record(
            BaseCvExtractionFailed(
                base_cv_id=self._id,
                reason=reason,
                occurred_at=at,
            )
        )

    @property
    def id(self) -> BaseCvId:
        return self._id

    @property
    def guest_session_id(self) -> GuestSessionId:
        return self._guest_session_id

    @property
    def original_filename(self) -> OriginalFilename:
        return self._original_filename

    @property
    def content_type(self) -> CvContentType:
        return self._content_type

    @property
    def size_bytes(self) -> int:
        return self._size_bytes

    @property
    def file(self) -> FileRef:
        return self._file

    @property
    def status(self) -> BaseCvStatus:
        return self._status

    @property
    def extracted_text(self) -> ExtractedText | None:
        return self._extracted_text

    @property
    def failure_reason(self) -> ExtractionFailureReason | None:
        return self._failure_reason

    @property
    def uploaded_at(self) -> datetime:
        return self._uploaded_at

    @property
    def extracted_at(self) -> datetime | None:
        return self._extracted_at
