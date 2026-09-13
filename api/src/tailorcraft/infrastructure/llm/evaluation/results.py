"""What one eval run records, and the summary computed across all of them.

Records, not rules: the decisions are in `checks.py` and the rendering is in `report.py`. Nothing here
holds a prompt; `PairResult.draft` holds the two tailored documents because a person is about to read
them, and they are generated from a synthetic corpus (AC-26) — never from a user's CV.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from tailorcraft.domain.tailoring.value_objects import TailoredDraft, TailoringFailureReason
from tailorcraft.infrastructure.llm.evaluation.checks import BudgetVerdict, nearest_rank
from tailorcraft.infrastructure.llm.evaluation.corpus import CorpusPair

OUTCOME_SUCCEEDED: Final = "succeeded"


class CheckStatus(StrEnum):
    PASS = "pass"  # noqa: S105 — a check outcome, not a password; bandit matches the word "pass".
    FAIL = "fail"
    # The check had nothing to look at — a failed run has no documents to measure.
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True, slots=True)
class SdkCall:
    """One attempt as `RecordingGenerate` saw it: how long, and what usage the provider reported.

    The token fields are `None` when the attempt raised (a timeout, a transport error, a refusal
    translated inside the SDK path) — there was no usage to read. `thoughts_tokens` is also `None` when
    a response arrived without the field, which is the expected shape with thinking disabled.
    """

    duration_ms: int
    error_type: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    thoughts_tokens: int | None


@dataclass(frozen=True, slots=True)
class PairResult:
    pair: CorpusPair
    # `succeeded`, a `TailoringFailureReason` value, or `port_contract_breach:<type>`.
    outcome: str
    # `LlmOutputInvalid.problem` when the output was refused by the parser.
    problem: str | None
    draft: TailoredDraft | None
    calls: tuple[SdkCall, ...]
    # All of `tailor()`, retries and backoff included: what a user would have waited for the model.
    wall_ms: int
    checks: tuple[CheckResult, ...]
    unfamiliar_phrases: tuple[str, ...]

    @property
    def llm_duration_ms(self) -> int | None:
        """What `tailoring_run.llm_duration_ms` would hold: the successful attempt's duration only."""
        return self.draft.metrics.duration_ms if self.draft is not None else None

    @property
    def failed_checks(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if check.status is CheckStatus.FAIL)


@dataclass(frozen=True, slots=True)
class Summary:
    pair_count: int
    succeeded_count: int
    failures: tuple[tuple[str, int], ...]
    call_count: int
    llm_duration_ms: tuple[int, ...]
    wall_ms: tuple[int, ...]
    mean_prompt_tokens: float | None
    mean_completion_tokens: float | None
    thoughts_absent_calls: int
    thoughts_zero_calls: int
    thoughts_nonzero: tuple[tuple[str, int], ...]
    unobserved_calls: int
    failed_check_count: int
    verdict: BudgetVerdict | None
    # Runs that failed because a deadline fired. Each missed the budget, and none is in
    # `llm_duration_ms` — the reason `render_summary` will not print WITHIN while this is non-zero.
    timed_out_count: int
    # Runs that succeeded only on a retry: in the duration sample, but over budget in wall time.
    retried_success_count: int

    @property
    def llm_p50_ms(self) -> int | None:
        return nearest_rank(self.llm_duration_ms, 50) if self.llm_duration_ms else None

    @property
    def wall_p50_ms(self) -> int | None:
        return nearest_rank(self.wall_ms, 50) if self.wall_ms else None

    @property
    def wall_p95_ms(self) -> int | None:
        return nearest_rank(self.wall_ms, 95) if self.wall_ms else None

    @property
    def exit_code(self) -> int:
        """1 when any cheap check failed (a failed run and non-zero thoughts included), when the budget
        is exceeded, or when there is no verdict at all. 0 otherwise — which says nothing about
        whether the documents are any good."""
        if self.failed_check_count or self.verdict is None or not self.verdict.within:
            return 1
        return 0


def summarise(results: Sequence[PairResult]) -> Summary:
    durations = tuple(d for d in (r.llm_duration_ms for r in results) if d is not None)
    drafts = [d for d in (r.draft for r in results) if d is not None]
    returned_thoughts = [
        call.thoughts_tokens for r in results for call in r.calls if call.error_type is None
    ]
    failures = Counter(
        f"{r.outcome} ({r.problem})" if r.problem else r.outcome for r in results if r.draft is None
    )
    return Summary(
        pair_count=len(results),
        succeeded_count=len(drafts),
        failures=tuple(sorted(failures.items())),
        call_count=sum(len(r.calls) for r in results),
        llm_duration_ms=durations,
        wall_ms=tuple(r.wall_ms for r in results),
        mean_prompt_tokens=(
            statistics.fmean(d.metrics.prompt_tokens for d in drafts) if drafts else None
        ),
        mean_completion_tokens=(
            statistics.fmean(d.metrics.completion_tokens for d in drafts) if drafts else None
        ),
        thoughts_absent_calls=sum(1 for t in returned_thoughts if t is None),
        thoughts_zero_calls=sum(1 for t in returned_thoughts if t == 0),
        thoughts_nonzero=_nonzero_thoughts(results),
        unobserved_calls=sum(1 for r in results for call in r.calls if call.error_type is not None),
        failed_check_count=sum(len(r.failed_checks) for r in results),
        verdict=(
            BudgetVerdict(p95_ms=nearest_rank(durations, 95), sample_size=len(durations))
            if durations
            else None
        ),
        timed_out_count=sum(
            1 for r in results if r.outcome == TailoringFailureReason.LLM_TIMED_OUT.value
        ),
        retried_success_count=sum(1 for r in results if r.draft is not None and len(r.calls) > 1),
    )


def _nonzero_thoughts(results: Sequence[PairResult]) -> tuple[tuple[str, int], ...]:
    found: list[tuple[str, int]] = []
    for result in results:
        for call in result.calls:
            tokens = call.thoughts_tokens
            if tokens:
                found.append((result.pair.id, tokens))
    return tuple(found)
