"""`ScoreComparison`: what a run's after-score says against its before-score (slice 5.2, plan §0.8)."""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.value_objects import ComparisonVerdict


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
        raise NotImplementedError
