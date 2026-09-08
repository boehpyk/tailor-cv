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

    # No `__post_init__` here on purpose: every `UUID` is already a valid `BaseCvId`, so a
    # validation method that does nothing would just be a place a future reader adds a rule that
    # does not belong. `GuestSessionId` (`domain/identity/value_objects.py`) is the same shape for
    # the same reason.


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
        # Deferred (function-local) import to break a module cycle: `domain/intake/errors.py`
        # imports `ExtractionFailureReason` from this module, so importing `errors` back at module
        # scope here would make the two modules import each other during collection — whichever
        # loads first would ask for names the other hasn't defined yet. Importing inside the method
        # instead defers the import to call time, by which point both modules have finished
        # loading. This is a local decision about *this* module's import shape, not a domain-purity
        # exception — `errors` is still `tailorcraft.domain`, so the purity test is unaffected.
        from tailorcraft.domain.intake.errors import InvalidFilename

        # Reduce to a basename by splitting on both `/` and `\` unconditionally. `os.path.basename`
        # alone would only split on `\` when the *server's* platform is Windows, but the browser can
        # send a Windows-style path regardless of where this process runs — the separator has to be
        # treated as a separator on principle, not on `os.sep`.
        basename = self.value.replace("\\", "/").rsplit("/", 1)[-1].strip()

        if not basename:
            raise InvalidFilename("filename must not be empty")
        if len(basename) > 255:
            raise InvalidFilename("filename must be at most 255 characters")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in basename):
            raise InvalidFilename("filename must not contain control characters or NUL")

        object.__setattr__(self, "value", basename)


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
        # See `OriginalFilename.__post_init__` for why this import is function-local rather than
        # module-level: `errors.py` imports `ExtractionFailureReason` from this module, so a
        # module-level import in the other direction would be a circular import at collection time.
        from tailorcraft.domain.intake.errors import EmptyExtraction, ExtractedTextTooShort

        # Whitespace normalization: `str.split()` with no argument already treats any run of
        # whitespace (spaces, tabs, newlines) as one separator and drops leading/trailing
        # whitespace, so re-joining with a single space collapses everything in one pass.
        normalized = " ".join(self.value.split())
        non_whitespace_count = sum(1 for char in normalized if not char.isspace())

        if not normalized:
            raise EmptyExtraction("extracted text must not be blank")
        if non_whitespace_count < 200:
            raise ExtractedTextTooShort(
                f"extracted text has only {non_whitespace_count} non-whitespace characters; "
                "the floor is 200 (OQ-9)"
            )

        object.__setattr__(self, "value", normalized)

    @property
    def character_count(self) -> int:
        """The count callers report to the user — so nobody downstream needs the text itself to
        say how much of it there is, which matters because the text is PII (Constitution §8).

        DECISION (made here, on purpose, per the T3 brief): the spec's validation floor and AC-1's
        reporting contract measure two different things. `__post_init__` enforces "≥ 200
        **non-whitespace** characters" as the validity floor — a document padded with 300 blank
        lines must not sneak past a 200-character minimum meant to guarantee real content. AC-1,
        separately, defines `character_count` as "the length of the extracted text". Those two
        counts diverge on any text that contains whitespace, so one of them has to give way, and it
        is written here rather than left for whoever next reads this to guess.

        `character_count` reports **the length of the normalized text** (whitespace collapsed to
        single spaces, ends stripped) — AC-1's reading, not the non-whitespace-only count used for
        the floor. Rationale: this is the number shown to the user ("4,821 characters extracted"),
        and a person counting characters in their own CV would count the spaces between words too.
        Reporting only non-whitespace characters would make the number on screen silently disagree
        with what they can see in the document itself, which is a worse failure than the two counts
        merely being *different numbers* internally.
        """
        return len(self.value)


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
        extensions = {
            CvContentType.PDF: "pdf",
            CvContentType.DOCX: "docx",
            CvContentType.TXT: "txt",
        }
        return extensions[self]


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
