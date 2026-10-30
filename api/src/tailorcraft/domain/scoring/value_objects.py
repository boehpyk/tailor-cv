"""Value objects for the `scoring` context: the texts judged, the judgement, and what it cost.

**Anything derived from a CV is `repr=False`:** the texts, the fingerprints, the gaps and the
assessment. A CV sits in a local of the API process for up to 14 s (ADR-0040), and a stray log line or
traceback must not be able to print it.

`ModelName` and `PromptVersion` are copied from `tailoring` on purpose: this context imports no
sibling (ADR-0040 decision 1), and sharing them would be coupling for a few lines of grammar.

Each `__post_init__` imports its error inside the method, because `errors` imports this module at the
top: the deferred import every other context uses for the same cycle.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_MIN_GAP_LENGTH = 3
_MAX_GAP_LENGTH = 120
_MAX_GAPS = 3
_MIN_SCORE_WITHOUT_GAPS = 90
_MAX_MODEL_NAME_LENGTH = 64
_PROMPT_VERSION = re.compile(r"[A-Za-z0-9._-]{1,16}")
_TOKENS_PER_PRICE_UNIT = 1_000_000

# AC-25's refusals, in order: a URL of any scheme, or starting `www.`; an email address; a run of 7
# or more digits, which a phone or an id would be; markup. A bare `evil.example` still passes: telling
# it from `Node.js` would need a list of domains.
_GAP_FORBIDDEN = re.compile(
    r"://|\bwww\.|[^\s@]+@[^\s@]+\.[^\s@]+|\d{7}|[<>]",
    re.IGNORECASE,
)


def _collapse_whitespace(text: str) -> str:
    """Every run of whitespace, line breaks included, becomes one space, and the ends go. So a flat
    `ExtractedText` and a Markdown tailored CV with the same words reach the same fingerprint."""
    return " ".join(text.split())


def _breaks_a_line_or_controls(char: str) -> bool:
    """A control character or a line or paragraph separator. A tab is neither here: it is whitespace
    to collapse, and it changes nothing about how a gap renders."""
    return char != "\t" and unicodedata.category(char) in {"Cc", "Zl", "Zp"}


def _is_count(value: object) -> bool:
    """A whole number, zero or more. `type`, not `isinstance`: a `bool` is an `int` to Python."""
    return type(value) is int and value >= 0


def _price_of(tokens: int, per_million: MicroUsd) -> MicroUsd:
    """Rounded up, in integers: a float would make the ledger's arithmetic inexact."""
    return MicroUsd(-(-tokens * per_million.value // _TOKENS_PER_PRICE_UNIT))


@dataclass(frozen=True, slots=True)
class FitScoreId:
    """A `FitScore`'s identity. Every `UUID` is a valid one, so there is nothing to validate."""

    value: UUID


@dataclass(frozen=True, slots=True)
class ScoredCvRef:
    """The base CV a score was made from, as this context's own type (`TrackedRunRef`'s reason).

    No foreign key behind it (ADR-0040 decision 8). It is what deletion matches on, never the
    score's identity, which is the two fingerprints.
    """

    value: UUID


@dataclass(frozen=True, slots=True)
class ScoredPostingRef:
    """The job posting a score was made from. See `ScoredCvRef`."""

    value: UUID


class FitScoreFailureReason(StrEnum):
    """Why an assessment failed. Closed, and **an error vocabulary only: never persisted**, because
    only a success is a row (ADR-0040 decision 2)."""

    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_RATE_LIMITED = "llm_rate_limited"
    LLM_TIMED_OUT = "llm_timed_out"
    LLM_REFUSED = "llm_refused"
    LLM_OUTPUT_INVALID = "llm_output_invalid"
    LLM_ERROR = "llm_error"
    INPUTS_TOO_LARGE = "inputs_too_large"


@dataclass(frozen=True, slots=True, repr=False)
class CvFingerprint:
    """SHA-256 of a CV's normalized text: 64 lowercase hex characters, else `InvalidFingerprint`.
    Never logged, evented or returned."""

    value: str

    def __post_init__(self) -> None:
        from tailorcraft.domain.scoring.errors import InvalidFingerprint

        if not _FINGERPRINT.fullmatch(self.value):
            raise InvalidFingerprint("a fingerprint must be 64 lowercase hex characters")


@dataclass(frozen=True, slots=True, repr=False)
class PostingFingerprint:
    """SHA-256 of a posting's normalized text. See `CvFingerprint`."""

    value: str

    def __post_init__(self) -> None:
        from tailorcraft.domain.scoring.errors import InvalidFingerprint

        if not _FINGERPRINT.fullmatch(self.value):
            raise InvalidFingerprint("a fingerprint must be 64 lowercase hex characters")


@dataclass(frozen=True, slots=True, repr=False)
class CvForScoring:
    """The CV text a score judges: whitespace collapsed, so a flat `ExtractedText` and a Markdown
    tailored CV with the same words reach the same prompt. Markdown markers stay (plan §0.8). Blank
    text is `InvalidScoringText`.

    Two types, this and `PostingForScoring`, so the arguments of `assess` cannot be swapped.
    """

    value: str

    def __post_init__(self) -> None:
        from tailorcraft.domain.scoring.errors import InvalidScoringText

        normalized = _collapse_whitespace(self.value)
        if not normalized:
            raise InvalidScoringText("a CV to score must not be blank")
        object.__setattr__(self, "value", normalized)

    @property
    def fingerprint(self) -> CvFingerprint:
        """Hashed on every read, so an async caller reads it inside its `to_thread` (AC-22)."""
        return CvFingerprint(hashlib.sha256(self.value.encode()).hexdigest())

    @property
    def character_count(self) -> int:
        return len(self.value)


@dataclass(frozen=True, slots=True, repr=False)
class PostingForScoring:
    """The posting text a score judges. See `CvForScoring`."""

    value: str

    def __post_init__(self) -> None:
        from tailorcraft.domain.scoring.errors import InvalidScoringText

        normalized = _collapse_whitespace(self.value)
        if not normalized:
            raise InvalidScoringText("a job posting to score must not be blank")
        object.__setattr__(self, "value", normalized)

    @property
    def fingerprint(self) -> PostingFingerprint:
        """Hashed on every read. See `CvForScoring.fingerprint`."""
        return PostingFingerprint(hashlib.sha256(self.value.encode()).hexdigest())

    @property
    def character_count(self) -> int:
        return len(self.value)


@dataclass(frozen=True, slots=True)
class ScorePercent:
    """The whole number 0 to 100, an estimate. A `bool` is refused: it is an `int` to Python and
    never a score. `InvalidScorePercent` otherwise."""

    value: int

    def __post_init__(self) -> None:
        from tailorcraft.domain.scoring.errors import InvalidScorePercent

        if not _is_count(self.value) or self.value > 100:
            raise InvalidScorePercent("a score must be a whole number from 0 to 100")


@dataclass(frozen=True, slots=True, repr=False)
class FitGap:
    """One requirement of the posting the CV does not show. Derived from a CV, so PII.

    AC-25: 3 to 120 characters after normalizing whitespace, one line, no control characters, and no
    URL, email address, run of 7 or more digits, or markup (`<`, `>`). `InvalidFitGap` otherwise, and
    the message never quotes the value.
    """

    value: str

    def __post_init__(self) -> None:
        from tailorcraft.domain.scoring.errors import InvalidFitGap

        # Before collapsing, which would quietly turn a line break or U+0085 into a space.
        if any(_breaks_a_line_or_controls(char) for char in self.value):
            raise InvalidFitGap("a gap must be one line with no control characters")
        normalized = _collapse_whitespace(self.value)
        if not _MIN_GAP_LENGTH <= len(normalized) <= _MAX_GAP_LENGTH:
            raise InvalidFitGap(
                f"a gap must be {_MIN_GAP_LENGTH} to {_MAX_GAP_LENGTH} characters, "
                f"got {len(normalized)}"
            )
        if _GAP_FORBIDDEN.search(normalized):
            raise InvalidFitGap(
                "a gap must not hold a URL, an email address, a run of 7 or more digits, or markup"
            )
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True, slots=True, repr=False)
class FitAssessment:
    """The score and its gaps, together. AC-27: at most 3 gaps, none only at a score of 90 or more,
    at least one below it, no duplicates when case-folded. `InvalidFitAssessment` otherwise. More
    than 3 is refused, never truncated."""

    score: ScorePercent
    gaps: tuple[FitGap, ...]

    def __post_init__(self) -> None:
        from tailorcraft.domain.scoring.errors import InvalidFitAssessment

        if len(self.gaps) > _MAX_GAPS:
            raise InvalidFitAssessment(f"an assessment holds at most {_MAX_GAPS} gaps")
        if not self.gaps and self.score.value < _MIN_SCORE_WITHOUT_GAPS:
            raise InvalidFitAssessment(
                f"a score below {_MIN_SCORE_WITHOUT_GAPS} must name at least one gap"
            )
        if len({gap.value.casefold() for gap in self.gaps}) != len(self.gaps):
            raise InvalidFitAssessment("an assessment must not repeat a gap")


@dataclass(frozen=True, slots=True)
class MicroUsd:
    """A whole number of micro-USD, zero or more, else `InvalidMicroUsd` (ADR-0041 decision 1)."""

    value: int

    def __post_init__(self) -> None:
        from tailorcraft.domain.scoring.errors import InvalidMicroUsd

        if not _is_count(self.value):
            raise InvalidMicroUsd("an amount must be a whole number of micro-USD, zero or more")

    def __add__(self, other: MicroUsd) -> MicroUsd:
        return MicroUsd(self.value + other.value)

    def __mul__(self, attempts: int) -> MicroUsd:
        """One attempt's worst case times the attempts allowed: the reservation (plan §2)."""
        return MicroUsd(self.value * attempts)


@dataclass(frozen=True, slots=True)
class ModelName:
    """Which model made a score, a provenance fact. 1 to 64 characters, no whitespace or control
    characters, stored exactly as given. `InvalidModelName` otherwise."""

    value: str

    def __post_init__(self) -> None:
        from tailorcraft.domain.scoring.errors import InvalidModelName

        if not 1 <= len(self.value) <= _MAX_MODEL_NAME_LENGTH:
            raise InvalidModelName(f"a model name must be 1 to {_MAX_MODEL_NAME_LENGTH} characters")
        if any(char.isspace() or unicodedata.category(char) == "Cc" for char in self.value):
            raise InvalidModelName("a model name must not hold whitespace or control characters")


@dataclass(frozen=True, slots=True)
class PromptVersion:
    """Which prompt made a score. 1 to 16 characters from `[A-Za-z0-9._-]`. `InvalidPromptVersion`
    otherwise."""

    value: str

    def __post_init__(self) -> None:
        from tailorcraft.domain.scoring.errors import InvalidPromptVersion

        if not _PROMPT_VERSION.fullmatch(self.value):
            raise InvalidPromptVersion("a prompt version must be 1 to 16 of [A-Za-z0-9._-]")


@dataclass(frozen=True, slots=True)
class ScoringUsage:
    """What one `assess` consumed across all its attempts, carried on a success and on the exception.

    `output_tokens` includes thoughts, which bill at the output rate. `unmetered_attempts` counts
    attempts that reported no usage: unknown is not zero (ADR-0041 decision 3). The three counts and
    `duration_ms` are zero or more, else `InvalidScoringUsage`. Zero everywhere is legal: a call that
    made no provider request settles to nothing.
    """

    input_tokens: int
    output_tokens: int
    unmetered_attempts: int
    duration_ms: int
    model: ModelName
    prompt_version: PromptVersion

    def __post_init__(self) -> None:
        from tailorcraft.domain.scoring.errors import InvalidScoringUsage

        counts = (
            self.input_tokens,
            self.output_tokens,
            self.unmetered_attempts,
            self.duration_ms,
        )
        if not all(_is_count(count) for count in counts):
            raise InvalidScoringUsage("usage counts and durations must be whole and not negative")


@dataclass(frozen=True, slots=True)
class ScoringPrices:
    """The price of a million tokens each way, **in micro-USD**, each above zero, else
    `InvalidScoringPrices`.

    The unit is load-bearing. ADR-0041's `ceil(tokens * price / 1e6)` yields micro-USD only when the
    price is in micro-USD per million: $0.10 per million is `MicroUsd(100_000)`, and 1,700 tokens cost
    170. Fed the setting's 0.10 in dollars, the same formula answers 1. Integers also keep the
    arithmetic exact. The composition root converts the `Decimal` settings once.
    """

    input_per_million: MicroUsd
    output_per_million: MicroUsd

    def __post_init__(self) -> None:
        from tailorcraft.domain.scoring.errors import InvalidScoringPrices

        if self.input_per_million.value == 0 or self.output_per_million.value == 0:
            raise InvalidScoringPrices("a price must be above zero")

    def worst_case_attempt(
        self, cv_chars: int, posting_chars: int, overhead: int, max_output_tokens: int
    ) -> MicroUsd:
        """One attempt's upper bound, taking one token to be at most one character (ADR-0041
        decision 2). Each direction rounds up on its own."""
        input_tokens = cv_chars + posting_chars + overhead
        return _price_of(input_tokens, self.input_per_million) + _price_of(
            max_output_tokens, self.output_per_million
        )

    def cost_of(self, usage: ScoringUsage, worst_attempt: MicroUsd) -> MicroUsd:
        """The actual cost: metered tokens each way, rounded up on their own, plus `worst_attempt`
        for each unmetered attempt (ADR-0041 decision 3)."""
        return (
            _price_of(usage.input_tokens, self.input_per_million)
            + _price_of(usage.output_tokens, self.output_per_million)
            + worst_attempt * usage.unmetered_attempts
        )


@dataclass(frozen=True, slots=True)
class AssessedFit:
    """What `FitScorerPort.assess` returns. A record of validated values, with nothing to check."""

    assessment: FitAssessment
    usage: ScoringUsage
