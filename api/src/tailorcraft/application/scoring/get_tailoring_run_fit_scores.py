"""The `GetTailoringRunFitScores` use case: a run's stored before/after and their verdict (plan §0.8)."""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.application.intake.get_base_cv import GetBaseCv
from tailorcraft.application.scoring.find_fit_score_for_pair import FindFitScoreForPair
from tailorcraft.application.scoring.resolve_scoring_pair import ResolveScoringPair
from tailorcraft.application.scoring.score_tailoring_run import draft_pair
from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRun
from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.scoring.comparison import ScoreComparison
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.ports import DraftTextPort
from tailorcraft.domain.scoring.value_objects import ComparisonUnavailableReason
from tailorcraft.domain.tailoring.value_objects import TailoringRunId, TailoringRunStatus


@dataclass(frozen=True, slots=True)
class GetTailoringRunFitScoresQuery:
    owner: Owner
    tailoring_run_id: TailoringRunId


@dataclass(frozen=True, slots=True)
class TailoringRunFitScores:
    """`comparable` is `None` unless both sides exist; `comparison` is set only when they are
    comparable (`ScoreComparison.between` is not `None`, S-9). `unavailable` says why there can be no
    comparison at all, and then both sides are `None`."""

    before: FitScore | None
    after: FitScore | None
    comparison: ScoreComparison | None
    comparable: bool | None
    unavailable: ComparisonUnavailableReason | None


class GetTailoringRunFitScores:
    """Stored hits only: **never calls the model**, never reserves, never counts.

    `get_run` (`TailoringRunNotFound`) -> not `SUCCEEDED` -> `unavailable=NOT_SUCCEEDED` ->
    `get_base_cv` missing -> `unavailable=BASE_CV_DELETED` -> the same two pairs `ScoreTailoringRun`
    builds -> `find_for_pair.for_pair` for each -> `ScoreComparison.between` when both exist.
    """

    def __init__(
        self,
        get_run: GetTailoringRun,
        get_base_cv: GetBaseCv,
        resolve_pair: ResolveScoringPair,
        find_for_pair: FindFitScoreForPair,
        draft_text: DraftTextPort,
    ) -> None:
        self._get_run = get_run
        self._get_base_cv = get_base_cv
        self._resolve_pair = resolve_pair
        self._find_for_pair = find_for_pair
        self._draft_text = draft_text

    async def __call__(self, query: GetTailoringRunFitScoresQuery) -> TailoringRunFitScores:
        owner = query.owner
        run = await self._get_run(query.tailoring_run_id, owner)
        if run.status is not TailoringRunStatus.SUCCEEDED:
            return _unavailable(ComparisonUnavailableReason.NOT_SUCCEEDED)
        try:
            await self._get_base_cv(run.base_cv_id, owner)
        except BaseCvNotFound:
            return _unavailable(ComparisonUnavailableReason.BASE_CV_DELETED)

        before_pair = await self._resolve_pair(owner, run.base_cv_id, run.job_posting_id)
        after_pair = await draft_pair(run, before_pair, self._draft_text)
        before = await self._find_for_pair.for_pair(owner, before_pair)
        after = await self._find_for_pair.for_pair(owner, after_pair)

        if before is None or after is None:
            return TailoringRunFitScores(before, after, None, None, None)
        comparison = ScoreComparison.between(before, after)
        return TailoringRunFitScores(before, after, comparison, comparison is not None, None)


def _unavailable(reason: ComparisonUnavailableReason) -> TailoringRunFitScores:
    return TailoringRunFitScores(
        before=None, after=None, comparison=None, comparable=None, unavailable=reason
    )
