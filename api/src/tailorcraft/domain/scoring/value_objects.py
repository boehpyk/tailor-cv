"""Value objects for the `scoring` context: the texts judged, the judgement, and what it cost.

SKELETON (T4): the signatures are real and `__post_init__` is a deliberate no-op, so a "refused" test
goes red on `DID NOT RAISE` rather than on an error. Behaviour arrives at T6.

**Anything derived from a CV is `repr=False`:** the texts, the fingerprints, the gaps and the
assessment. A CV sits in a local of the API process for up to 14 s (ADR-0040), and a stray log line or
traceback must not be able to print it.

`ModelName` and `PromptVersion` are copied from `tailoring` on purpose: this context imports no
sibling (ADR-0040 decision 1), and sharing them would be coupling for a few lines of grammar.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


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
        """No-op until T6."""


@dataclass(frozen=True, slots=True, repr=False)
class PostingFingerprint:
    """SHA-256 of a posting's normalized text. See `CvFingerprint`."""

    value: str

    def __post_init__(self) -> None:
        """No-op until T6."""


@dataclass(frozen=True, slots=True, repr=False)
class CvForScoring:
    """The CV text a score judges: whitespace collapsed, so a flat `ExtractedText` and a Markdown
    tailored CV with the same words reach the same prompt. Markdown markers stay (plan §0.8). Blank
    text is `InvalidScoringText`.

    Two types, this and `PostingForScoring`, so the arguments of `assess` cannot be swapped.
    """

    value: str

    def __post_init__(self) -> None:
        """No-op until T6."""

    @property
    def fingerprint(self) -> CvFingerprint:
        raise NotImplementedError

    @property
    def character_count(self) -> int:
        raise NotImplementedError


@dataclass(frozen=True, slots=True, repr=False)
class PostingForScoring:
    """The posting text a score judges. See `CvForScoring`."""

    value: str

    def __post_init__(self) -> None:
        """No-op until T6."""

    @property
    def fingerprint(self) -> PostingFingerprint:
        raise NotImplementedError

    @property
    def character_count(self) -> int:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ScorePercent:
    """The whole number 0 to 100, an estimate. A `bool` is refused: it is an `int` to Python and
    never a score. `InvalidScorePercent` otherwise."""

    value: int

    def __post_init__(self) -> None:
        """No-op until T6."""


@dataclass(frozen=True, slots=True, repr=False)
class FitGap:
    """One requirement of the posting the CV does not show. Derived from a CV, so PII.

    AC-25: 3 to 120 characters after normalizing whitespace, one line, no control characters, and no
    URL, email address, run of 7 or more digits, or markup (`<`, `>`). `InvalidFitGap` otherwise, and
    the message never quotes the value.
    """

    value: str

    def __post_init__(self) -> None:
        """No-op until T6."""


@dataclass(frozen=True, slots=True, repr=False)
class FitAssessment:
    """The score and its gaps, together. AC-27: at most 3 gaps, none only at a score of 90 or more,
    at least one below it, no duplicates when case-folded. `InvalidFitAssessment` otherwise. More
    than 3 is refused, never truncated."""

    score: ScorePercent
    gaps: tuple[FitGap, ...]

    def __post_init__(self) -> None:
        """No-op until T6."""


@dataclass(frozen=True, slots=True)
class MicroUsd:
    """A whole number of micro-USD, zero or more, else `InvalidMicroUsd` (ADR-0041 decision 1)."""

    value: int

    def __post_init__(self) -> None:
        """No-op until T6."""

    def __add__(self, other: MicroUsd) -> MicroUsd:
        raise NotImplementedError

    def __mul__(self, attempts: int) -> MicroUsd:
        """One attempt's worst case times the attempts allowed: the reservation (plan §2)."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ModelName:
    """Which model made a score, a provenance fact. 1 to 64 characters, no whitespace or control
    characters, stored exactly as given. `InvalidModelName` otherwise."""

    value: str

    def __post_init__(self) -> None:
        """No-op until T6."""


@dataclass(frozen=True, slots=True)
class PromptVersion:
    """Which prompt made a score. 1 to 16 characters from `[A-Za-z0-9._-]`. `InvalidPromptVersion`
    otherwise."""

    value: str

    def __post_init__(self) -> None:
        """No-op until T6."""


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
        """No-op until T6."""


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
        """No-op until T6."""

    def worst_case_attempt(
        self, cv_chars: int, posting_chars: int, overhead: int, max_output_tokens: int
    ) -> MicroUsd:
        """One attempt's upper bound, taking one token to be at most one character (ADR-0041
        decision 2). Each direction rounds up on its own."""
        raise NotImplementedError

    def cost_of(self, usage: ScoringUsage, worst_attempt: MicroUsd) -> MicroUsd:
        """The actual cost: metered tokens each way, rounded up on their own, plus `worst_attempt`
        for each unmetered attempt (ADR-0041 decision 3)."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class AssessedFit:
    """What `FitScorerPort.assess` returns. A record of validated values, with nothing to check."""

    assessment: FitAssessment
    usage: ScoringUsage
