"""The `ScoreTailoringRun` use case: one side of a run's before/after comparison (plan §0.8)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from tailorcraft.application.intake.get_base_cv import GetBaseCv
from tailorcraft.application.scoring.find_fit_score_for_pair import FindFitScoreForPair
from tailorcraft.application.scoring.request_fit_score import (
    RequestFitScore,
    RequestFitScoreCommand,
    RequestOutcome,
)
from tailorcraft.application.scoring.resolve_scoring_pair import ResolveScoringPair, ScoringPair
from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRun
from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.scoring.errors import ComparisonUnavailable
from tailorcraft.domain.scoring.ports import DraftTextPort
from tailorcraft.domain.scoring.value_objects import (
    ComparisonSide,
    ComparisonUnavailableReason,
    CvForScoring,
    ScoredRunRef,
    ScoringPurpose,
)
from tailorcraft.domain.tailoring.errors import TailoringRunNotSucceeded
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunId, TailoringRunStatus


def _with_cv(pair: ScoringPair, text: str) -> ScoringPair:
    """The CPU-shaped part, for `asyncio.to_thread` (`ResolveScoringPair`'s reason, AC-22)."""
    cv = CvForScoring(text)
    return replace(pair, cv=cv, cv_fingerprint=cv.fingerprint)


async def draft_pair(
    run: TailoringRun, before: ScoringPair, draft_text: DraftTextPort
) -> ScoringPair:
    """`before` with its CV replaced by the run's **draft** as plain text, never the revision
    (ADR-0015). The base CV's `cv_id` stays, so deleting the CV takes the draft's score too.
    Shared with `GetTailoringRunFitScores`, so the read finds exactly the pair this one stores."""
    documents = run.documents
    if documents is None:  # a SUCCEEDED run always has them; this narrows for the type checker
        raise TailoringRunNotSucceeded(run.id, run.status)
    text = await draft_text.to_plain_text(documents.cv.value)
    return await asyncio.to_thread(_with_cv, before, text)


@dataclass(frozen=True, slots=True)
class ScoreTailoringRunCommand:
    owner: Owner
    tailoring_run_id: TailoringRunId
    side: ComparisonSide


class ScoreTailoringRun:
    """Score the run's base CV (`BEFORE`) or its draft CV (`AFTER`) against the run's posting.

    Flow, and **the order is the error order** of the comparison `POST` (plan §4):

    1. `get_run(run_id, owner)`: `TailoringRunNotFound` (404, not mine included).
    2. Not `SUCCEEDED` -> `TailoringRunNotSucceeded` (S-5).
    3. `get_base_cv(run.base_cv_id, owner)`: `BaseCvNotFound` becomes
       `ComparisonUnavailable(BASE_CV_DELETED)` (S-6).
    4. `pair = resolve_pair(owner, run.base_cv_id, run.job_posting_id)` (`BaseCvNotReadyForScoring`
       propagates). For `AFTER`, the pair's CV is replaced by
       `draft_text.to_plain_text(run.documents.cv.value)`, **the draft, never the revision**
       (ADR-0015), keeping the base CV's `cv_id` so a CV deletion takes the score.
    5. `find_for_pair.for_pair(owner, pair)`: a hit is `RequestOutcome(hit, created=False)` (200).
    6. Miss: `request_fit_score(RequestFitScoreCommand(owner, pair, purpose=COMPARISON,
       draft_of=ScoredRunRef(run.id.value) for AFTER, else None))`. Its errors propagate
       (`FitScoreSpendCapReached`, `FitScoringFailed` incl. S-10's `INPUTS_TOO_LARGE`, and a
       store refusal when the run, CV or posting went during the call, S-7).
    """

    def __init__(
        self,
        get_run: GetTailoringRun,
        get_base_cv: GetBaseCv,
        resolve_pair: ResolveScoringPair,
        find_for_pair: FindFitScoreForPair,
        request_fit_score: RequestFitScore,
        draft_text: DraftTextPort,
    ) -> None:
        self._get_run = get_run
        self._get_base_cv = get_base_cv
        self._resolve_pair = resolve_pair
        self._find_for_pair = find_for_pair
        self._request_fit_score = request_fit_score
        self._draft_text = draft_text

    async def __call__(self, cmd: ScoreTailoringRunCommand) -> RequestOutcome:
        run = await self._get_run(cmd.tailoring_run_id, cmd.owner)
        if run.status is not TailoringRunStatus.SUCCEEDED:
            raise TailoringRunNotSucceeded(run.id, run.status)
        try:
            await self._get_base_cv(run.base_cv_id, cmd.owner)
        except BaseCvNotFound:
            raise ComparisonUnavailable(ComparisonUnavailableReason.BASE_CV_DELETED) from None

        pair = await self._resolve_pair(cmd.owner, run.base_cv_id, run.job_posting_id)
        draft_of: ScoredRunRef | None = None
        if cmd.side is ComparisonSide.AFTER:
            pair = await draft_pair(run, pair, self._draft_text)
            draft_of = ScoredRunRef(run.id.value)

        stored = await self._find_for_pair.for_pair(cmd.owner, pair)
        if stored is not None:
            return RequestOutcome(stored=stored, created=False)
        return await self._request_fit_score(
            RequestFitScoreCommand(
                owner=cmd.owner,
                pair=pair,
                purpose=ScoringPurpose.COMPARISON,
                draft_of=draft_of,
            )
        )
