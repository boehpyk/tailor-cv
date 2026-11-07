"""`ScoreComparison`: what a run's after-score says against its before-score (slice 5.2, plan §0.8)."""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.value_objects import SAME_JUDGEMENT_BAND, ComparisonVerdict


@dataclass(frozen=True, slots=True)
class ScoreComparison:
    """`delta` is `after - before` in points; `verdict` reads it against `SAME_JUDGEMENT_BAND`."""

    delta: int
    verdict: ComparisonVerdict

    @classmethod
    def between(cls, before: FitScore, after: FitScore) -> ScoreComparison | None:
        """`None` when the two were not judged the same way: a different model or prompt version
        (OQ-4). Otherwise `delta = after - before`, and the verdict is `ABOUT_THE_SAME` within the
        band either side, inclusive, `HIGHER` or `LOWER` beyond it."""
        before_usage, after_usage = before.usage, after.usage
        if (before_usage.model, before_usage.prompt_version) != (
            after_usage.model,
            after_usage.prompt_version,
        ):
            return None
        delta = after.assessment.score.value - before.assessment.score.value
        if delta > SAME_JUDGEMENT_BAND:
            verdict = ComparisonVerdict.HIGHER
        elif delta < -SAME_JUDGEMENT_BAND:
            verdict = ComparisonVerdict.LOWER
        else:
            verdict = ComparisonVerdict.ABOUT_THE_SAME
        return cls(delta=delta, verdict=verdict)
