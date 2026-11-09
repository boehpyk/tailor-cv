"""RED (slice 5.2 T10): `ScoreComparison.between` (plan section 0.8) and `FitScore.draft_of`.

Pure: no I/O. Scores are built through `FitScore.from_assessment`, never by reaching into fields.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.scoring.comparison import ScoreComparison
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.value_objects import (
    AssessedFit,
    ComparisonVerdict,
    CvFingerprint,
    FitAssessment,
    FitGap,
    FitScoreId,
    MicroUsd,
    ModelName,
    PostingFingerprint,
    PromptVersion,
    ScoredCvRef,
    ScoredPostingRef,
    ScoredRunRef,
    ScorePercent,
    ScoringUsage,
)

_AT = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
_RUN = ScoredRunRef(UUID("66666666-6666-7666-8666-666666666666"))


def _score(
    percent: int,
    *,
    model: str = "gemini-x",
    prompt: str = "1",
    draft_of: ScoredRunRef | None = None,
) -> FitScore:
    usage = ScoringUsage(
        input_tokens=1_000,
        output_tokens=400,
        unmetered_attempts=0,
        duration_ms=1_500,
        model=ModelName(model),
        prompt_version=PromptVersion(prompt),
    )
    return FitScore.from_assessment(
        id=FitScoreId(UUID("0192f0a1-89ab-7cde-8123-456789abcde0")),
        owner=UserOwner(UserId(UUID("55555555-5555-7555-8555-555555555555"))),
        base_cv_id=ScoredCvRef(UUID("22222222-2222-7222-8222-222222222222")),
        job_posting_id=ScoredPostingRef(UUID("33333333-3333-7333-8333-333333333333")),
        cv_fingerprint=CvFingerprint("a" * 64),
        posting_fingerprint=PostingFingerprint("b" * 64),
        assessed=AssessedFit(
            FitAssessment(ScorePercent(percent), (FitGap("a gap"),) if percent < 90 else ()), usage
        ),
        cost=MicroUsd(100),
        at=_AT,
        draft_of=draft_of,
    )


@pytest.mark.parametrize(
    ("after", "delta", "verdict"),
    [
        (44, -6, ComparisonVerdict.LOWER),
        (45, -5, ComparisonVerdict.ABOUT_THE_SAME),
        (50, 0, ComparisonVerdict.ABOUT_THE_SAME),
        (55, 5, ComparisonVerdict.ABOUT_THE_SAME),
        (56, 6, ComparisonVerdict.HIGHER),
    ],
)
def test_delta_is_after_minus_before_and_read_against_the_band(
    after: int, delta: int, verdict: ComparisonVerdict
) -> None:
    result = ScoreComparison.between(_score(50), _score(after))

    assert result == ScoreComparison(delta=delta, verdict=verdict)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (_score(50, model="gemini-x"), _score(60, model="gemini-y")),
        (_score(50, prompt="1"), _score(60, prompt="2")),
    ],
    ids=["model-differs", "prompt-version-differs"],
)
def test_scores_judged_differently_are_not_comparable(before: FitScore, after: FitScore) -> None:
    assert ScoreComparison.between(before, after) is None


def test_a_fit_score_carries_the_run_whose_draft_it_judged() -> None:
    assert _score(50, draft_of=_RUN).draft_of == _RUN


def test_a_fit_score_of_a_base_cv_has_no_draft_of() -> None:
    assert _score(50).draft_of is None
