"""Errors the `intake` bounded context raises when a business rule about a base CV is broken.

Unlike the value objects and the aggregate, these carry no behaviour to defer to a later step —
an error class *is* its contract, so it gets a real body here rather than a `NotImplementedError`
stub. The domain never raises `HTTPException` and never carries a status code; translating one of
these into a response is the API layer's job (see `docs/specs/intake-base-cv-upload/technical-plan.md`
for the status-code mapping).
"""

from __future__ import annotations

from tailorcraft.domain.intake.value_objects import BaseCvStatus, ExtractionFailureReason
from tailorcraft.domain.shared.errors import DomainError


class BaseCvNotFound(DomainError):
    """No `BaseCv` exists with the requested id."""


class BaseCvNotOwnedBySession(DomainError):
    """A `BaseCv` exists, but for a different session than the one asking.

    The API layer maps this to the same 404 as `BaseCvNotFound` (F-20, ADR-0008): a guest session id
    is not authority over an object that references it, and telling an attacker "wrong session"
    instead of "not found" would leak that the id exists. The distinction stays two error types
    rather than one, though, because the use case's own tests need to tell "absent" from "not mine"
    apart even if the HTTP response cannot.
    """


class BaseCvNotOwnedByUser(DomainError):
    """A `BaseCv` exists, but its owner is not the signed-in user asking — a guest-owned CV or
    another user's (slice 2.2, AC-8).

    The user-path twin of `BaseCvNotOwnedBySession`, with the same shape: the use case raises
    `BaseCvNotFound` **from** this, so the HTTP answer is the same 404 as a nonexistent id and an
    attacker learns nothing about which ids exist, while the use case's tests can still tell
    "absent" from "not mine" on `__cause__`.
    """


class ExtractionAlreadyDecided(DomainError):
    """`mark_extracted` or `mark_extraction_failed` was called on a `BaseCv` whose extraction
    outcome is already recorded. Extraction is decided exactly once (invariant I-3) — a second call,
    from a retry or a race, must not silently overwrite the first decision."""


class InvalidFilename(DomainError):
    """`OriginalFilename` was given something that cannot be a display name: empty after
    `strip()`, longer than 255 characters, or carrying a path separator, a control character or a
    NUL — the last three because a filename is a label, never a path (ADR-0011)."""


class InvalidFileRef(DomainError):
    """A `FileRef` key does not match the storage grammar. The grammar makes a traversing key
    unrepresentable, so this fires only for a key built somewhere it should never have been built
    by hand — evidence of a bug, not user input (ADR-0011)."""


class EmptyExtraction(DomainError):
    """Extraction produced no text at all: a blank or whitespace-only string."""


class ExtractedTextTooShort(DomainError):
    """Extraction produced text, but fewer than `ExtractedText`'s floor of non-whitespace
    characters (OQ-9) — not enough for a model to tailor against, so it is treated as a failure at
    the value-object boundary rather than a partial success the caller has to remember to check."""


class TooManyBaseCvs(DomainError):
    """The session already owns `max_base_cvs_per_session` base CVs (F-23).

    This is a use-case check, not an invariant of `BaseCv` — the rule spans every `BaseCv` a session
    owns, which is a fact `BaseCv` itself has no way to know. A soft cap: concurrent requests can
    overshoot it by a little, and that overshoot is accepted rather than locked (OQ-10).
    """


class CvExtractionFailed(DomainError):
    """Base for every way `CvTextExtractorPort.extract` can fail.

    Carries the `ExtractionFailureReason` the use case needs to call `BaseCv.mark_extraction_failed`
    — the one thing this exception exists to communicate. Per ADR-0004, a failed extraction is a
    recorded state of the aggregate, never a 500 with nothing on disk: the use case catches this,
    the failure does not propagate past it.
    """

    def __init__(self, reason: ExtractionFailureReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class EncryptedCvFile(CvExtractionFailed):
    """The PDF is password-protected; the extractor cannot read past its own encryption."""

    def __init__(self) -> None:
        super().__init__(ExtractionFailureReason.ENCRYPTED)


class CorruptCvFile(CvExtractionFailed):
    """The file cannot be parsed as the format its bytes claim to be — a truncated PDF, a bad zip,
    a DOCX with no `word/document.xml`."""

    def __init__(self) -> None:
        super().__init__(ExtractionFailureReason.CORRUPT)


class CvHasNoTextLayer(CvExtractionFailed):
    """The file parses, but yields no text — the common case being a scanned PDF: pages that are
    images with no text layer for `pypdf` to read."""

    def __init__(self) -> None:
        super().__init__(ExtractionFailureReason.NO_TEXT_LAYER)


class CvTextTooShort(CvExtractionFailed):
    """The file parses and yields text, but fewer than `ExtractedText`'s floor — the extraction
    itself succeeded; the content was not enough of a CV to tailor against."""

    def __init__(self) -> None:
        super().__init__(ExtractionFailureReason.TOO_SHORT)


class CvHasTooManyPages(CvExtractionFailed):
    """The PDF exceeds `max_cv_pages`, refused before parsing starts — bounded work rather than a
    timeout discovered the hard way on a 400-page file."""

    def __init__(self) -> None:
        super().__init__(ExtractionFailureReason.TOO_MANY_PAGES)


class CvTextTooLong(CvExtractionFailed):
    """The file parses, but its text runs past the extracted-character cap
    (`Settings.max_extracted_characters`) — refused part-way through extraction, not after it, so a
    small archive that inflates to tens of millions of characters is never built into one string
    (slice 2.2, T30b-C). Carries no count: the reason is the contract, and a number measured from a
    stranger's document is one more fact about it than the use case needs."""

    def __init__(self) -> None:
        super().__init__(ExtractionFailureReason.TEXT_TOO_LONG)


class CvExtractionTimedOut(CvExtractionFailed):
    """Extraction ran past `extraction_timeout_seconds`. Reported under the general
    `EXTRACTOR_ERROR` reason — there is no separate "timed out" value in `ExtractionFailureReason`
    because the user-facing message is the same either way: the file could not be read in time."""

    def __init__(self) -> None:
        super().__init__(ExtractionFailureReason.EXTRACTOR_ERROR)


# --------------------------------------------------------------------------------------------------
# Slice 2.2 — saved base CVs.
# --------------------------------------------------------------------------------------------------


class TooManySavedBaseCvs(DomainError):
    """The user already keeps `max_saved_base_cvs_per_user` saved base CVs (OQ-3).

    `TooManyBaseCvs`'s sibling for the other owner, and a use-case check for the same reason: the rule
    spans every `BaseCv` a user owns, which no single `BaseCv` can see. Soft, like 1.1's F-23 cap —
    a concurrent upload can overshoot it by one, and that is accepted rather than locked.
    """


class SavedBaseCvNotCopyable(DomainError):
    """A saved base CV cannot be copied into the workspace because its extraction has not succeeded
    (`uploaded` or `extraction_failed`) — a working copy is only ever `EXTRACTED`, since the copy
    never re-runs the extractor (AC-9, I-8).

    Carries the source's status: the one fact the caller needs to say *why*, and a closed enum, so
    there is nowhere in it for text from the document to travel.
    """

    def __init__(self, status: BaseCvStatus) -> None:
        super().__init__(status.value)
        self.status = status


class SavedBaseCvFileMissing(DomainError):
    """The saved base CV's row exists but its stored file does not (S-32).

    Distinct from `BaseCvNotFound` on purpose: after `StoredFileMissing` the use case re-reads the
    row, and a row that is still there while its bytes are gone is a bug somewhere — worth its own
    answer ("delete it and upload it again") rather than a 404 that says it never existed.
    """


class InvalidLabel(DomainError):
    """`BaseCvLabel` was given something that cannot be a display name: empty after `strip()`,
    longer than 80 code points, or carrying a control character or a NUL (AC-4)."""
