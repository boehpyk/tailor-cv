"""The `BaseCv` aggregate: a user's source CV as uploaded, and everything this slice knows about it.

Composes `RecordsEvents` (`domain/shared/events.py`) rather than inheriting a shared aggregate base
class — CLAUDE.md is explicit that two aggregates with the same shape do not get a base class, since
shared shape is not shared behaviour and a base class ends up guessing at rules that differ between
`BaseCv` and the future `TailoringRun`.
"""

from __future__ import annotations

from datetime import datetime

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    BaseCvStatus,
    CvContentType,
    ExtractedText,
    ExtractionFailureReason,
    OriginalFilename,
)
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

    # Class-level annotations only (no assignment): with no `__init__` override, this is how
    # mypy --strict learns the attribute types that `upload` sets directly on `self` and the
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

    # No `__init__` override. `upload` is the only way application code builds a valid instance —
    # calling `BaseCv(...)` directly falls through to `object.__init__`, which rejects any keyword
    # argument, so "the only constructor is `upload`" is enforced by the absence of a constructor
    # here rather than by convention. SQLAlchemy's imperative mapping does not need `__init__`
    # either: it rehydrates a mapped instance by instrumenting `__dict__` directly and never calls it
    # on the load path (ADR-0007).

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
        raise NotImplementedError

    def mark_extracted(self, text: ExtractedText, at: datetime) -> None:
        """Record that extraction succeeded: sets `status = EXTRACTED`, `extracted_text = text`,
        `extracted_at = at`, and records `BaseCvTextExtracted`.

        Raises `ExtractionAlreadyDecided` (I-3) if extraction was already decided, and
        `InvariantViolated` (I-4) if `at < uploaded_at`.
        """
        raise NotImplementedError

    def mark_extraction_failed(self, reason: ExtractionFailureReason, at: datetime) -> None:
        """Record that extraction failed: sets `status = EXTRACTION_FAILED`,
        `failure_reason = reason`, `extracted_at = at`, and records `BaseCvExtractionFailed`.

        Raises `ExtractionAlreadyDecided` (I-3) if extraction was already decided, and
        `InvariantViolated` (I-4) if `at < uploaded_at`. This is the ADR-0004 shape: a failed
        extraction is a recorded state of the aggregate, never an exception that escapes to a 500.
        """
        raise NotImplementedError

    @property
    def id(self) -> BaseCvId:
        raise NotImplementedError

    @property
    def guest_session_id(self) -> GuestSessionId:
        raise NotImplementedError

    @property
    def original_filename(self) -> OriginalFilename:
        raise NotImplementedError

    @property
    def content_type(self) -> CvContentType:
        raise NotImplementedError

    @property
    def size_bytes(self) -> int:
        raise NotImplementedError

    @property
    def file(self) -> FileRef:
        raise NotImplementedError

    @property
    def status(self) -> BaseCvStatus:
        raise NotImplementedError

    @property
    def extracted_text(self) -> ExtractedText | None:
        raise NotImplementedError

    @property
    def failure_reason(self) -> ExtractionFailureReason | None:
        raise NotImplementedError

    @property
    def uploaded_at(self) -> datetime:
        raise NotImplementedError

    @property
    def extracted_at(self) -> datetime | None:
        raise NotImplementedError
