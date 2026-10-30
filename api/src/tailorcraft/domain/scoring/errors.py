"""Errors the `scoring` bounded context raises.

An error class *is* its contract, so every one has a real body at the skeleton step. **Nothing here
carries text:** no CV, posting, gap, quote or provider message. Ids, counts, closed values and fixed
labels only (Constitution §8). The API layer maps these to status codes.

`value_objects` imports these inside `__post_init__`, since this module imports it at the top: the
deferred import every other context uses for the same cycle.
"""

from __future__ import annotations

from datetime import datetime

from tailorcraft.domain.scoring.value_objects import FitScoreFailureReason, ScoringUsage
from tailorcraft.domain.shared.errors import DomainError


class FitScoringFailed(DomainError):
    """`FitScorerPort.assess` raises this on every failure, and the adapter's `except Exception` floor
    makes that true by construction. `Exception`, never `BaseException`: a cancellation must cancel.

    Carries `usage` because a failed call may still have been billed, and the ledger settles to it
    (ADR-0041 decision 3). It propagates to the router, since a failure leaves no row.
    """

    def __init__(self, reason: FitScoreFailureReason, usage: ScoringUsage) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.usage = usage


class FitScoringOutputInvalid(FitScoringFailed):
    """The model answered and the answer was unusable (S-4, S-5). `problem` is a fixed label from the
    parser's vocabulary, never the offending text."""

    def __init__(self, problem: str, usage: ScoringUsage) -> None:
        super().__init__(FitScoreFailureReason.LLM_OUTPUT_INVALID, usage)
        self.problem = problem


class TooManyFitScores(DomainError):
    """The owner is at their limit of stored scores (AC-8, AC-12). A use-case check, and soft: calls in
    flight are not rows yet.

    `retry_not_before` is the oldest counted score plus 24 hours for a user, and `None` for a guest,
    whose limit never frees up.
    """

    def __init__(self, count: int, limit: int, retry_not_before: datetime | None) -> None:
        super().__init__(f"owner already has {count} fit scores; the limit is {limit}")
        self.count = count
        self.limit = limit
        self.retry_not_before = retry_not_before


class FitScoreSpendCapReached(DomainError):
    """The day's ledger cannot take this reservation, so no call is made (S-10, ADR-0041 decision 6)."""


class InvalidScoringText(DomainError):
    """A CV or posting to score is blank after normalizing whitespace."""


class InvalidFingerprint(DomainError):
    """A fingerprint is not 64 lowercase hex characters."""


class InvalidScorePercent(DomainError):
    """A score is not a whole number from 0 to 100, or is a `bool`."""


class InvalidFitGap(DomainError):
    """A gap breaks AC-25. The message names the rule and never quotes the value."""


class InvalidFitAssessment(DomainError):
    """A score and its gaps break AC-27 together."""


class InvalidMicroUsd(DomainError):
    """An amount of money is negative."""


class InvalidModelName(DomainError):
    """A model name is blank, longer than 64 characters, or holds whitespace or a control character."""


class InvalidPromptVersion(DomainError):
    """A prompt version is blank, longer than 16 characters, or holds a character outside
    `[A-Za-z0-9._-]`."""


class InvalidScoringUsage(DomainError):
    """A token count, an attempt count or a duration is negative."""


class InvalidScoringPrices(DomainError):
    """A price is not above zero (AC-18)."""
