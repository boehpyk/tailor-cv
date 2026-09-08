"""Value objects for the `intake` bounded context: what makes an uploaded CV a valid one.

Every field of `BaseCv` that has a rule gets its own type here instead of staying a bare `str` or
`int`. A primitive with a rule enforced somewhere else is a bug waiting for a second caller who does
not know the rule exists; a value object makes the invalid state unrepresentable at the type level —
`ExtractedText("")` cannot be constructed, so no code downstream has to check for it again.

All frozen `@dataclass(frozen=True, slots=True)`, validating in `__post_init__`, compared by value.
Never Pydantic (ADR-0002): a `BaseModel` here would drag JSON aliases and `model_config` into
business rules that have nothing to do with the HTTP boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


@dataclass(frozen=True, slots=True)
class BaseCvId:
    """A `BaseCv`'s identity, typed so a signature cannot silently accept the wrong UUID.

    `uuid` is standard library, so the domain-purity rule allows it — the type is `BaseCvId`, not
    `UUID`, precisely so that `GuestSessionId` and `BaseCvId` cannot be swapped by a caller who got
    the argument order wrong; the mistake becomes a `mypy --strict` error instead of a runtime bug
    that only shows up as one guest reading another guest's CV.
    """

    value: UUID

    def __post_init__(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class OriginalFilename:
    """The filename as the user typed it — a display label, never a path.

    ADR-0011 is explicit that this value is never joined to a filesystem path: a user-supplied name
    is a path-traversal attempt looking for somewhere to be concatenated, and here there is nowhere.
    Validated to 1-255 characters after `strip()`, rejecting path separators, control characters and
    NUL, and reduced to a basename before it ever reaches this type.
    """

    value: str

    def __post_init__(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ExtractedText:
    """The text `CvTextExtractorPort` pulled out of an uploaded file.

    Non-blank and at least 200 non-whitespace characters after whitespace normalization (OQ-9: the
    floor is provisional, chosen rather than measured — verify it against the fixture corpus and
    change it once, on purpose, rather than letting a test quietly ratify whatever the corpus
    produces). Raises `EmptyExtraction` or `ExtractedTextTooShort`. The aggregate can never hold an
    invalid extraction because the type that carries it cannot exist in an invalid state — invariant
    I-5 is enforced here, not in `BaseCv`.
    """

    value: str

    def __post_init__(self) -> None:
        raise NotImplementedError

    @property
    def character_count(self) -> int:
        """The count callers report to the user — so nobody downstream needs the text itself to
        say how much of it there is, which matters because the text is PII (Constitution §8)."""
        raise NotImplementedError


class CvContentType(StrEnum):
    """The three formats intake accepts. An enum, not a value object: the set is closed and finite,
    decided by `sniff_cv_content_type` from the file's bytes — never from the client's `Content-Type`
    header or the filename's extension — so a value object's `__post_init__` would have nothing left
    to validate."""

    PDF = "application/pdf"
    DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    TXT = "text/plain"

    @property
    def file_extension(self) -> str:
        """The extension `FileRef.for_base_cv` puts on the storage key — `pdf`, `docx` or `txt`,
        never re-derived from the original filename (ADR-0011)."""
        raise NotImplementedError


class BaseCvStatus(StrEnum):
    """Where a `BaseCv` stands on the one thing this slice tracks: has extraction been decided.

    `EXTRACTED` iff `extracted_text is not None`; `EXTRACTION_FAILED` iff `failure_reason is not
    None`; never both (invariant I-2). `mark_extracted` and `mark_extraction_failed` are the only
    two writers, and each is callable exactly once (invariant I-3).
    """

    UPLOADED = "uploaded"
    EXTRACTED = "extracted"
    EXTRACTION_FAILED = "extraction_failed"


class ExtractionFailureReason(StrEnum):
    """Why `CvTextExtractorPort.extract` failed, recorded on the aggregate as a state rather than
    left as an escaped exception (ADR-0004) — the same shape `TailoringRun` needs in 1.3 for a
    failed LLM call. Each member has exactly one `CvExtractionFailed` subclass that binds it in
    `domain/intake/errors.py`."""

    ENCRYPTED = "encrypted"
    CORRUPT = "corrupt"
    NO_TEXT_LAYER = "no_text_layer"
    TOO_SHORT = "too_short"
    TOO_MANY_PAGES = "too_many_pages"
    EXTRACTOR_ERROR = "extractor_error"
