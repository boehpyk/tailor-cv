"""Domain events for the `scoring` bounded context.

An event's field set **is** a log field set: `LoggingEventPublisher` logs every field. So there is no
owner, score, gap, quote or fingerprint here. Pure data, so complete at the skeleton step.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.scoring.value_objects import FitScoreId, ModelName, PromptVersion
from tailorcraft.domain.shared.events import DomainEvent


@dataclass(frozen=True, slots=True, kw_only=True)
class FitScoreRecorded(DomainEvent):
    """A new assessment of a pair was made and is about to be stored. Published only if this one is
    the row stored, never by the loser of a two-tab race (ADR-0040 decision 6).

    Metrics are flat ints, as `TailoringRunSucceeded`'s are, so a log query can aggregate over them.
    """

    fit_score_id: FitScoreId
    gap_count: int
    input_tokens: int
    output_tokens: int
    cost_microusd: int
    duration_ms: int
    model: ModelName
    prompt_version: PromptVersion
