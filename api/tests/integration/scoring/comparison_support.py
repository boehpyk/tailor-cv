"""Arrangement for slice 5.2's comparison tests (T15): a `World` plus the run's side of it.

An account (`UserOwner`) with a saved CV, a posting and a **succeeded** run over them whose draft CV
is deliberately not the base CV's text, so a test can tell which of the two a use case scored.
`BEFORE_TEXT` is the base CV, `DRAFT_MARKDOWN` the model's draft; a revision, when a test makes one,
is a third text. The draft goes through `FakeDraftText` (the real TXT renderer, as the real adapter
does), whose output a test computes the same way, because the port's output *is* the definition of
what is scored.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from tailorcraft.application.intake.get_base_cv import GetBaseCv
from tailorcraft.application.scoring.get_tailoring_run_fit_scores import GetTailoringRunFitScores
from tailorcraft.application.scoring.resolve_scoring_pair import ScoringPair
from tailorcraft.application.scoring.score_tailoring_run import ScoreTailoringRun
from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRun
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.value_objects import (
    CvForScoring,
    FitScoreId,
    MicroUsd,
    ModelName,
    ScoredRunRef,
)
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    TailoredCv,
    TailoredDocuments,
    TailoringFailureReason,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import FakeDraftText, FakeTailoringRunRepository
from tests.integration.owners import a_draft, queued_run
from tests.integration.scoring.support import (
    MAX_ATTEMPTS,
    OVERHEAD,
    USUAL_COST,
    World,
    assessed,
    extracted_cv_with,
    usage,
)

BEFORE_TEXT = "alpha " * 60
DRAFT_MARKDOWN = "# Delta Person\n\n" + "- delta delta delta delta delta delta delta delta\n" * 12
REVISION_MARKDOWN = (
    "# Epsilon Person\n\n" + "- epsilon epsilon epsilon epsilon epsilon epsilon\n" * 12
)


POSTING_CHARS = len("post " * 40) - 1  # `pasted_posting`'s text, whitespace-collapsed


def reserved_for(cv_text: str) -> int:
    """The reservation for this CV text against `inputs()`' posting, worked from the formula by hand
    (integer ceilings, not `ScoringPrices`): MAX_ATTEMPTS * (ceil(in * 0.1) + 205)."""
    cv_chars = len(" ".join(cv_text.split()))
    input_tokens = cv_chars + POSTING_CHARS + OVERHEAD
    return MAX_ATTEMPTS * (-(-input_tokens // 10) + 205)


class ComparisonWorld(World):
    def __init__(self, fixed: FixedClock, **kwargs: object) -> None:
        super().__init__(fixed, **kwargs)  # type: ignore[arg-type]
        self.runs = FakeTailoringRunRepository()
        self.draft_text = FakeDraftText()
        self.owner: UserOwner | None = None
        self.cv_id: BaseCvId | None = None
        self.run: TailoringRun | None = None

    # --- arrangement ---

    async def succeeded_run(self) -> TailoringRun:
        """The user, a saved CV holding `BEFORE_TEXT`, a posting, and a succeeded run over them."""
        owner = await self.user()
        cv = extracted_cv_with(owner, self.earlier, BEFORE_TEXT)
        await self.cvs.add(cv)
        posting_id = (await self.inputs(owner))[1]
        run = queued_run(owner, self.earlier, base_cv_id=cv.id, job_posting_id=posting_id)
        run.mark_started(self.earlier)
        run.mark_succeeded(
            TailoredDocuments(cv=TailoredCv(DRAFT_MARKDOWN), cover_letter=CoverLetter("l" * 300)),
            a_draft().metrics,
            self.earlier,
        )
        run.release_events()
        await self.runs.add(run)
        self.owner, self.cv_id, self.run = owner, cv.id, run
        return run

    async def revise(self, run: TailoringRun) -> None:
        run.revise_cv(TailoredCv(REVISION_MARKDOWN), expected_version=run.version, at=self.earlier)
        run.release_events()

    async def failed_run(self) -> TailoringRun:
        run = await self.succeeded_run()
        owner = self.owner
        assert owner is not None
        failed = queued_run(
            owner, self.earlier, base_cv_id=run.base_cv_id, job_posting_id=run.job_posting_id
        )
        failed.mark_failed(TailoringFailureReason.NOT_QUEUED, self.earlier)
        failed.release_events()
        await self.runs.add(failed)
        return failed

    # --- the two pairs a run names ---

    async def before_pair(self, run: TailoringRun) -> ScoringPair:
        assert self.owner is not None
        return await self.resolve()(self.owner, run.base_cv_id, run.job_posting_id)

    async def after_pair(self, run: TailoringRun, markdown: str = DRAFT_MARKDOWN) -> ScoringPair:
        before = await self.before_pair(run)
        cv = CvForScoring(await FakeDraftText().to_plain_text(markdown))
        return replace(before, cv=cv, cv_fingerprint=cv.fingerprint)

    def seed_before(self, pair: ScoringPair, score: int, *, model: str = "gemini-test") -> FitScore:
        return self._seed(pair, score, None, model=model)

    def seed_after(
        self, run: TailoringRun, pair: ScoringPair, score: int, *, model: str = "gemini-test"
    ) -> FitScore:
        return self._seed(pair, score, ScoredRunRef(run.id.value), model=model)

    def _seed(
        self,
        pair: ScoringPair,
        score: int,
        draft_of: ScoredRunRef | None,
        *,
        model: str = "gemini-test",
        at: datetime | None = None,
    ) -> FitScore:
        assert self.owner is not None
        row = FitScore.from_assessment(
            id=FitScoreId(value=uuid4()),
            owner=self.owner,
            base_cv_id=pair.cv_id,
            job_posting_id=pair.posting_id,
            cv_fingerprint=pair.cv_fingerprint,
            posting_fingerprint=pair.posting_fingerprint,
            assessed=assessed(score, used=replace(usage(), model=ModelName(model))),
            cost=MicroUsd(USUAL_COST),
            at=at or self.earlier,
            draft_of=draft_of,
        )
        row.release_events()
        self.scores.seed(row)
        return row

    # --- the use cases ---

    def get_run(self) -> GetTailoringRun:
        return GetTailoringRun(self.runs, self.sessions, self.users, self.fixed)

    def score_run(self, **config: object) -> ScoreTailoringRun:
        return ScoreTailoringRun(
            get_run=self.get_run(),
            get_base_cv=GetBaseCv(self.cvs, self.sessions, self.users, self.fixed),
            resolve_pair=self.resolve(),
            find_for_pair=self.find(),
            request_fit_score=self.request(**config),  # type: ignore[arg-type]
            draft_text=self.draft_text,
        )

    def fit_scores(self) -> GetTailoringRunFitScores:
        return GetTailoringRunFitScores(
            get_run=self.get_run(),
            get_base_cv=GetBaseCv(self.cvs, self.sessions, self.users, self.fixed),
            resolve_pair=self.resolve(),
            find_for_pair=self.find(),
            draft_text=self.draft_text,
        )
