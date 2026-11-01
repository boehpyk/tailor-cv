"""The fit-score measurement behind `make eval.score` (`tailorcraft.cli eval-fit-scores`, T43).

**Not a test, and it costs money.** It runs the committed synthetic corpus through the real
`GeminiFitScorer` (pre-flight, deadline, retries, parser, accounting) and measures what no offline test
can (AC-50 to AC-54):

- **Stability (AC-51, gated):** each corpus pair scored `--repeat` times; `max - min <= 5`.
- **Discrimination (AC-52, gated):** each CV's matched pair (its median) at least 20 points above
  its one mismatched cross pair. A scorer answering 70 every time passes AC-51; this catches it.
- **Injection (AC-29, OQ-14):** two synthetic postings that try to steer the scorer. Reported only.
- **Cost (AC-53), thinking and refusals (AC-54):** reported, with the thresholds flagged.

**Without `--confirm-spend` it makes no call**: it prints the plan and the upper bound at the
settings' prices, then exits 0. With it, exit 0 means AC-51 and AC-52 held, 1 that one did not, and 2
that it could not start.

**It prints scores, gap counts, tokens and labels, never a gap, a quote, a prompt or a completion**,
although the corpus is synthetic: the habit is the guard, and this file is the template the next
measurement copies.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final, TextIO

from tailorcraft.domain.scoring.errors import FitScoringFailed, FitScoringOutputInvalid
from tailorcraft.domain.scoring.value_objects import (
    CvForScoring,
    MicroUsd,
    PostingForScoring,
    ScoringPrices,
    ScoringUsage,
)
from tailorcraft.infrastructure.api.deps import fit_score_prices
from tailorcraft.infrastructure.llm.evaluation.checks import nearest_rank
from tailorcraft.infrastructure.llm.evaluation.corpus import (
    CorpusError,
    FitScoreCorpus,
    load_fit_score_corpus,
)
from tailorcraft.infrastructure.llm.evaluation.results import SdkCall
from tailorcraft.infrastructure.llm.evaluation.runner import (
    RecordingGenerate,
    route_logs_to_stderr,
)
from tailorcraft.infrastructure.llm.fit_score_parsing import (
    FitScoreResponseInvalid,
    parse_fit_score_response,
)
from tailorcraft.infrastructure.llm.fit_score_prompt import FIT_SCORE_PROMPT_VERSION
from tailorcraft.infrastructure.llm.fit_scorer import (
    PROMPT_OVERHEAD_CHARACTERS,
    GeminiFitScorer,
)
from tailorcraft.infrastructure.llm.gemini import GenerateFn, RawCompletion
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.settings import Settings, get_settings

KIND_STABILITY: Final = "stability"
KIND_CROSS: Final = "cross"
KIND_INJECTED: Final = "injected"

OUTCOME_SUCCEEDED: Final = "succeeded"
VERDICT_PARSED: Final = "parsed"

MAX_SCORE_RANGE: Final = 5  # AC-51
MIN_DISCRIMINATION: Final = 20  # AC-52
MAX_REFUSAL_RATE: Final = Decimal("0.10")  # AC-54
MAX_INPUT_TOKENS_PER_CHARACTER: Final = 1.0  # AC-54: the reservation's 1 token <= 1 character
PRD_MEAN_COST: Final = MicroUsd(1_000)  # AC-53: $0.001, reported, not gated

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_CANNOT_START: Final = 2


@dataclass(frozen=True, slots=True)
class ScoreJob:
    """One score to take. `key` groups it: a pair id, a CV id (cross) or an injected id."""

    kind: str
    key: str
    label: str
    cv: CvForScoring
    posting: PostingForScoring

    @property
    def input_characters(self) -> int:
        """What the reservation counts as the prompt's size (ADR-0041 decision 2)."""
        return self.cv.character_count + self.posting.character_count + PROMPT_OVERHEAD_CHARACTERS


@dataclass(frozen=True, slots=True)
class ScoreResult:
    job: ScoreJob
    # `succeeded`, a `FitScoreFailureReason` value, or `llm_output_invalid:<problem>`.
    outcome: str
    score: int | None
    gap_count: int | None
    usage: ScoringUsage
    cost: MicroUsd
    calls: tuple[SdkCall, ...]
    # The parser's verdict on each completion that arrived: `parsed` or a `PROBLEM_*` label.
    verdicts: tuple[str, ...]


def plan_jobs(corpus: FitScoreCorpus, repeat: int) -> tuple[ScoreJob, ...]:
    """AC-50's run, in order: every corpus pair `repeat` times, then the cross pairs, then the
    injected pairs, once each."""
    jobs: list[ScoreJob] = []
    for pair in corpus.base.pairs:
        cv, posting = CvForScoring(pair.cv.text.value), PostingForScoring(pair.posting.text.value)
        jobs.extend(
            ScoreJob(KIND_STABILITY, pair.id, f"pair {pair.id} run {n}/{repeat}", cv, posting)
            for n in range(1, repeat + 1)
        )
    jobs.extend(
        ScoreJob(
            KIND_CROSS,
            cross.cv.id,
            f"cross {cross.cv.id} x {cross.posting.id}",
            CvForScoring(cross.cv.text.value),
            PostingForScoring(cross.posting.text.value),
        )
        for cross in corpus.cross
    )
    jobs.extend(
        ScoreJob(
            KIND_INJECTED,
            injected.id,
            f"injected {injected.id} ({injected.cv.id})",
            CvForScoring(injected.cv.text.value),
            PostingForScoring(injected.posting.text.value),
        )
        for injected in corpus.injected
    )
    return tuple(jobs)


def worst_attempt(job: ScoreJob, settings: Settings, prices: ScoringPrices) -> MicroUsd:
    return prices.worst_case_attempt(
        job.cv.character_count,
        job.posting.character_count,
        PROMPT_OVERHEAD_CHARACTERS,
        settings.fit_score_max_output_tokens,
    )


def upper_bound(jobs: Sequence[ScoreJob], settings: Settings, prices: ScoringPrices) -> MicroUsd:
    """What the ledger would reserve for every job: each attempt allowed at its worst case."""
    total = MicroUsd(0)
    for job in jobs:
        total = total + worst_attempt(job, settings, prices) * settings.fit_score_max_attempts
    return total


class ParserVerdicts:
    """A `GenerateFn` that forwards, then runs the parser over each completion for its label only.

    The adapter retries an invalid answer once, so its outcome hides a first-attempt refusal that a
    retry repaired; AC-54's refusal rate is per call. The text is parsed and dropped, never kept.
    """

    __slots__ = ("_inner", "_posting", "_verdicts")

    def __init__(self, inner: GenerateFn, posting: str) -> None:
        self._inner = inner
        self._posting = posting
        self._verdicts: list[str] = []

    @property
    def verdicts(self) -> tuple[str, ...]:
        return tuple(self._verdicts)

    async def __call__(self, prompt: str) -> RawCompletion:
        completion = await self._inner(prompt)
        try:
            parse_fit_score_response(completion.text, self._posting)
        except FitScoreResponseInvalid as invalid:
            self._verdicts.append(invalid.problem)
        else:
            self._verdicts.append(VERDICT_PARSED)
        return completion


async def score_once(
    job: ScoreJob, *, settings: Settings, prices: ScoringPrices, generate: GenerateFn
) -> ScoreResult:
    """One `assess` through the real adapter, with the recorder and the verdicts on its seam."""
    recorder = RecordingGenerate(generate)
    verdicts = ParserVerdicts(recorder, job.posting.value)
    scorer = GeminiFitScorer(settings, generate=verdicts)
    score: int | None = None
    gap_count: int | None = None
    try:
        assessed = await scorer.assess(job.cv, job.posting)
    except FitScoringOutputInvalid as invalid:
        outcome, usage = f"{invalid.reason.value}:{invalid.problem}", invalid.usage
    except FitScoringFailed as failure:
        outcome, usage = failure.reason.value, failure.usage
    else:
        outcome, usage = OUTCOME_SUCCEEDED, assessed.usage
        score, gap_count = assessed.assessment.score.value, len(assessed.assessment.gaps)
    return ScoreResult(
        job=job,
        outcome=outcome,
        score=score,
        gap_count=gap_count,
        usage=usage,
        cost=prices.cost_of(usage, worst_attempt(job, settings, prices)),
        calls=recorder.calls,
        verdicts=verdicts.verdicts,
    )


async def run_fit_score_eval(
    corpus: FitScoreCorpus,
    settings: Settings,
    *,
    out: TextIO,
    repeat: int,
    generate: GenerateFn | None = None,
) -> int:
    """Take every planned score, print each line and the summary, return the exit code.

    `generate` is the testing seam with **the real SDK path as its strict default**; `make eval.score`
    never passes it. Sequential on purpose: concurrency would measure the provider's rate limiting.
    """
    prices = fit_score_prices(settings)
    real: GeminiFitScorer | None = None
    if generate is None:
        real = GeminiFitScorer(settings)
        generate = real.generate
    results: list[ScoreResult] = []
    try:
        for job in plan_jobs(corpus, repeat):
            result = await score_once(job, settings=settings, prices=prices, generate=generate)
            _print_result(out, result)
            results.append(result)
    finally:
        if real is not None:
            await real.aclose()
    return render_summary(out, corpus, results, repeat=repeat)


def run_from_cli(*, corpus_dir: Path, repeat: int, confirm_spend: bool, validate_only: bool) -> int:
    """`tailorcraft.cli eval-fit-scores`. The corpus is validated and the plan printed before the key
    is looked at, so neither needs one, and nothing is spent unless `--confirm-spend` says so."""
    settings = get_settings()
    configure_logging(settings)
    route_logs_to_stderr()
    if repeat < 2:
        print("eval-fit-scores: --repeat must be at least 2 to measure stability", file=sys.stderr)
        return EXIT_CANNOT_START
    try:
        corpus = load_fit_score_corpus(corpus_dir, max_cv_characters=settings.llm_max_cv_characters)
    except CorpusError as error:
        print(f"eval-fit-scores: {error}", file=sys.stderr)
        return EXIT_CANNOT_START
    out = sys.stdout
    print(
        f"corpus: {len(corpus.base.pairs)} pairs, {len(corpus.cross)} cross pairs, "
        f"{len(corpus.injected)} injected pairs ({len(corpus.base.cvs)} CVs)",
        file=out,
    )
    if validate_only:
        print("corpus is valid; no API call was made.", file=out)
        return EXIT_OK
    jobs = plan_jobs(corpus, repeat)
    render_plan(out, jobs, settings, repeat=repeat)
    if not confirm_spend:
        print("\nNo call was made. Re-run with --confirm-spend to spend it.", file=out)
        return EXIT_OK
    if not settings.gemini_api_key.strip():
        print(
            "eval-fit-scores: GEMINI_API_KEY is empty, so every score would fail as "
            "llm_unavailable without a call. Set it in the root .env and re-run.",
            file=sys.stderr,
        )
        return EXIT_CANNOT_START
    return asyncio.run(run_fit_score_eval(corpus, settings, out=out, repeat=repeat))


# -- report ----------------------------------------------------------------------------------------


def render_plan(out: TextIO, jobs: Sequence[ScoreJob], settings: Settings, *, repeat: int) -> None:
    counts = Counter(job.kind for job in jobs)
    attempts = settings.fit_score_max_attempts
    print(
        f"model {settings.fit_score_model}, prompt version {FIT_SCORE_PROMPT_VERSION}\n"
        f"plan: {counts[KIND_STABILITY] // repeat} pairs x {repeat} repeats = "
        f"{counts[KIND_STABILITY]}, {counts[KIND_CROSS]} cross pairs x 1, "
        f"{counts[KIND_INJECTED]} injected pairs x 1: {len(jobs)} scores, "
        f"at most {len(jobs) * attempts} calls ({attempts} attempts each)\n"
        f"upper bound: {_usd(upper_bound(jobs, settings, fit_score_prices(settings)))} at "
        f"${settings.fit_score_input_price_usd_per_million} in / "
        f"${settings.fit_score_output_price_usd_per_million} out per million tokens "
        "(the settings' prices: verify them on Google's pricing page first, T50). "
        "It assumes 1 token <= 1 character, which AC-54 checks.",
        file=out,
    )


def _print_result(out: TextIO, result: ScoreResult) -> None:
    score = "-" if result.score is None else f"{result.score:3d}"
    gaps = "-" if result.gap_count is None else str(result.gap_count)
    print(
        f"{result.job.label:<56} {result.outcome:<28} score {score} gaps {gaps} "
        f"in {result.usage.input_tokens:,} out {result.usage.output_tokens:,} "
        f"cost {_usd(result.cost)} {result.usage.duration_ms:,} ms "
        f"[{', '.join(result.verdicts) or 'no completion'}]",
        file=out,
    )


def render_summary(
    out: TextIO, corpus: FitScoreCorpus, results: Sequence[ScoreResult], *, repeat: int
) -> int:
    """Print every AC's numbers, return 0 when AC-51 and AC-52 both held, else 1."""
    stable = _render_stability(out, corpus, results, repeat=repeat)
    discriminating = _render_discrimination(out, corpus, results)
    _render_injected(out, corpus, results)
    _render_cost(out, results)
    _render_calls(out, results)
    passed = stable and discriminating
    print(
        f"\nverdict: AC-51 {'PASS' if stable else 'FAIL'}, AC-52 "
        f"{'PASS' if discriminating else 'FAIL'}" + ("" if passed else " -- no release; see OQ-13"),
        file=out,
    )
    return EXIT_OK if passed else EXIT_FAILED


def _scores(results: Sequence[ScoreResult], kind: str, key: str) -> list[int]:
    return [
        r.score for r in results if r.job.kind == kind and r.job.key == key and r.score is not None
    ]


def _render_stability(
    out: TextIO, corpus: FitScoreCorpus, results: Sequence[ScoreResult], *, repeat: int
) -> bool:
    print(f"\nAC-51 stability (max - min <= {MAX_SCORE_RANGE} over {repeat} repeats)", file=out)
    passed = True
    for pair in corpus.base.pairs:
        scores = _scores(results, KIND_STABILITY, pair.id)
        if len(scores) < repeat:
            # A repeat that produced no score is not a stable answer, and its range is unmeasured.
            ok, detail = False, f"only {len(scores)} of {repeat} scored"
        else:
            spread = max(scores) - min(scores)
            ok, detail = spread <= MAX_SCORE_RANGE, f"range {spread}"
        passed = passed and ok
        print(f"  pair {pair.id}: {scores} {detail} {'PASS' if ok else 'FAIL'}", file=out)
    return passed


def _render_discrimination(
    out: TextIO, corpus: FitScoreCorpus, results: Sequence[ScoreResult]
) -> bool:
    print(f"\nAC-52 discrimination (matched median - mismatched >= {MIN_DISCRIMINATION})", file=out)
    passed = True
    for cross in corpus.cross:
        matched = _scores(results, KIND_STABILITY, cross.matched.id)
        mismatched = _scores(results, KIND_CROSS, cross.cv.id)
        if not matched or not mismatched:
            ok, detail = False, "unmeasured: a score is missing"
        else:
            median = statistics.median(matched)
            difference = median - mismatched[0]
            ok = difference >= MIN_DISCRIMINATION
            detail = (
                f"pair {cross.matched.id} median {median} vs {cross.posting.id} "
                f"{mismatched[0]}: {difference}"
            )
        passed = passed and ok
        print(f"  {cross.cv.id}: {detail} {'PASS' if ok else 'FAIL'}", file=out)
    return passed


def _render_injected(out: TextIO, corpus: FitScoreCorpus, results: Sequence[ScoreResult]) -> None:
    print("\nAC-29 injected pairs (reported, not gated: OQ-14)", file=out)
    for injected in corpus.injected:
        for r in results:
            if r.job.kind == KIND_INJECTED and r.job.key == injected.id:
                print(
                    f"  {injected.id} ({injected.cv.id}): {r.outcome}, score {r.score}, "
                    f"gaps {r.gap_count}. It tries: {injected.tries}",
                    file=out,
                )


def _render_cost(out: TextIO, results: Sequence[ScoreResult]) -> None:
    costs = [r.cost.value for r in results]
    if not costs:
        return
    mean = MicroUsd(-(-sum(costs) // len(costs)))
    print(
        f"\nAC-53 cost per score, at the settings' prices: mean {_usd(mean)}, "
        f"p95 {_usd(MicroUsd(nearest_rank(costs, 95)))}, max {_usd(MicroUsd(max(costs)))}, "
        f"total {_usd(MicroUsd(sum(costs)))} over {len(costs)} scores",
        file=out,
    )
    if mean.value > PRD_MEAN_COST.value:
        print(
            f"  the mean is above the PRD's {_usd(PRD_MEAN_COST)}: report it to the owner", file=out
        )
    print("  next day: compare the total with Google's usage or billing view.", file=out)


def _render_calls(out: TextIO, results: Sequence[ScoreResult]) -> None:
    calls = [call for r in results for call in r.calls]
    returned = [call for call in calls if call.error_type is None]
    thinking = [call.thoughts_tokens for call in returned if call.thoughts_tokens]
    print(
        f"\nAC-54 thinking: {len(thinking)} of {len(returned)} returned calls reported thoughts "
        f"{'FAIL' if thinking else 'PASS'}",
        file=out,
    )

    verdicts: Counter[str] = Counter(v for r in results for v in r.verdicts)
    errors = Counter(call.error_type for call in calls if call.error_type is not None)
    total = len(calls)
    print(f"AC-54 per call ({total} calls): parser verdicts, then calls that raised", file=out)
    for label, count in (*verdicts.most_common(), *errors.most_common()):
        print(f"  {label}: {count} ({_percent(count, total)})", file=out)
    refused = verdicts.total() - verdicts[VERDICT_PARSED]
    if total and Decimal(refused) / total > MAX_REFUSAL_RATE:
        print(
            f"  parser refusals {_percent(refused, total)} are above {MAX_REFUSAL_RATE:.0%}: "
            "a prompt defect to fix before release",
            file=out,
        )

    ratios = [
        call.prompt_tokens / r.job.input_characters
        for r in results
        for call in r.calls
        if call.prompt_tokens
    ]
    if ratios:
        highest = max(ratios)
        verdict = "PASS" if highest <= MAX_INPUT_TOKENS_PER_CHARACTER else "FAIL"
        print(
            f"AC-54 highest input tokens per character: {highest:.3f} {verdict}"
            + ("" if verdict == "PASS" else " -- the reservation is no upper bound (ADR-0041)"),
            file=out,
        )

    durations = [r.usage.duration_ms for r in results]
    if durations:
        print(
            f"provider time per score: p50 {nearest_rank(durations, 50):,} ms, "
            f"p95 {nearest_rank(durations, 95):,} ms",
            file=out,
        )


def _usd(amount: MicroUsd) -> str:
    return f"${Decimal(amount.value) / 1_000_000:.6f}"


def _percent(part: int, whole: int) -> str:
    return f"{Decimal(part) / whole:.1%}" if whole else "n/a"
