"""Ports the `scoring` context needs from the outside world, in the domain's own language.

No red-first cycle: a `Protocol` has no behaviour to fail (docs/sdlc.md §2). The adapters satisfy these
structurally under `mypy --strict`, and `test_port_bindings.py` holds each one to a composition root.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol

from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.value_objects import (
    AssessedFit,
    CvFingerprint,
    CvForScoring,
    FitScoreId,
    MicroUsd,
    PostingFingerprint,
    PostingForScoring,
)


class FitScorerPort(Protocol):
    """The judgement of one CV against one posting. Knows no model, schema, retry or JSON."""

    async def assess(self, cv: CvForScoring, posting: PostingForScoring) -> AssessedFit:
        """Raises `FitScoringFailed` on every failure, carrying what the attempts consumed, because
        a failed call may still have been billed (ADR-0041 decision 3)."""
        ...


class FitScoreRepository(Protocol):
    """Persistence for `FitScore`. One row per owner and pair (ADR-0040 decision 3)."""

    def next_identity(self) -> FitScoreId:
        """Mint an id for a score that does not exist yet. Needs no I/O (UUIDv7, ADR-0007)."""
        ...

    async def store(self, score: FitScore) -> FitScore:
        """Store `score` and return the row now stored: this one, or the winner's of a two-tab race.

        Raises `BaseCvNotFound`, `JobPostingNotFound`, `GuestSessionExpired` or `UserNotFound` when
        what the score points at went during the call. Nothing is written, and the surrounding
        transaction survives, so the settlement after it still commits. Those errors belong to other
        contexts, so they are named here and never imported (ADR-0040 decision 1).
        """
        ...

    async def find_for_pair(
        self, owner: Owner, cv_fingerprint: CvFingerprint, posting_fingerprint: PostingFingerprint
    ) -> FitScore | None:
        """The owner's stored score of this pair, or `None`. Another owner's is never found."""
        ...

    async def scored_at_since(self, owner: Owner, since: datetime | None) -> Sequence[datetime]:
        """When each of the owner's scores was made, oldest first, from `since` on. `None` counts
        every score: a guest's limit never frees up (AC-8, AC-12)."""
        ...


class SpendLedgerPort(Protocol):
    """The day's spend on scoring, one number per UTC day (ADR-0041). Each method's return is the
    durability point, so the use case's call order is its transaction boundary (plan §0.2)."""

    async def try_reserve(self, day: date, amount: MicroUsd, cap: MicroUsd) -> bool:
        """Add `amount` unless that passes `cap`. `True` is durable, and `False` changed nothing."""
        ...

    async def settle(self, day: date, reserved: MicroUsd, actual: MicroUsd) -> None:
        """Replace a reservation with what the call cost, on the reservation's `day`, so a call
        across midnight adjusts the right one. Durable on return."""
        ...
