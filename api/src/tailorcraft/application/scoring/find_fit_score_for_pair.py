"""The `FindFitScoreForPair` use case: the owner's stored score of a pair, or nothing."""

from __future__ import annotations

from tailorcraft.application.scoring.resolve_scoring_pair import ResolveScoringPair, ScoringPair
from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.ports import FitScoreRepository


class FindFitScoreForPair:
    """Two entry points, because the two routes start from different places.

    `GET` has only ids, and "not scored yet" is its ordinary answer: `__call__`. `POST` has already
    resolved the pair, whose not-extracted error it needs as a 409, and must not hash a CV twice:
    `for_pair`. Neither touches Redis, the limiter or the ledger (AC-9).
    """

    def __init__(self, scores: FitScoreRepository, resolve_pair: ResolveScoringPair) -> None:
        self._scores = scores
        self._resolve_pair = resolve_pair

    async def __call__(
        self, owner: Owner, base_cv_id: BaseCvId, job_posting_id: JobPostingId
    ) -> FitScore | None:
        """`GET`. The CV and posting are still authorized, so `BaseCvNotFound` and
        `JobPostingNotFound` propagate (S-22, S-23). `BaseCvNotReadyForScoring` becomes `None`
        (S-24's 200 null): a CV with no text has no score, and a page that is only asking has done
        nothing wrong."""
        raise NotImplementedError

    async def for_pair(self, owner: Owner, pair: ScoringPair) -> FitScore | None:
        """`POST`'s stored-hit check, before the limiter: `FitScoreRepository.find_for_pair`, by the
        pair's fingerprints."""
        raise NotImplementedError
