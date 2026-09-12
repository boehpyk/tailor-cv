"""Rendering the eval for a human: the corpus, each pair side by side, and the summary.

Plain text to stdout, so `make eval > eval-report.txt` produces a file any editor opens and two
reports from two prompt versions can be diffed. Log lines go to stderr (`runner.run_from_cli`), so
they never interleave with the report in that file.
"""

from __future__ import annotations

import textwrap
from collections.abc import Sequence
from itertools import zip_longest
from typing import Final, TextIO

from tailorcraft.infrastructure.llm.evaluation.checks import (
    CONSTITUTION_BUDGET_MS,
    PLUMBING_BOUND_MS,
)
from tailorcraft.infrastructure.llm.evaluation.corpus import Corpus, CorpusPair
from tailorcraft.infrastructure.llm.evaluation.results import (
    PairResult,
    SdkCall,
    Summary,
)
from tailorcraft.infrastructure.llm.prompt import PROMPT_VERSION, build_tailoring_prompt
from tailorcraft.infrastructure.settings import Settings

DEFAULT_WIDTH: Final = 160
_MIN_COLUMN: Final = 30
_GUTTER: Final = " | "
_MAX_INDENT: Final = 8

_JUDGEMENT_GUIDE: Final = """\
What to judge in every pair (the part no check can do):
  1. Does the tailored CV answer THIS posting's actual requirements, or generic ones?
  2. Is every employer, title, date, degree, certification and metric traceable to the base CV?
     The employer check only knows names the corpus declares; an invented one is yours to spot.
     "Unfamiliar phrases" lists capitalised phrases the base CV never uses, to help you look.
  3. Where the posting asks for something the candidate lacks ("watch for"), is it left out or
     framed honestly, and never claimed?
  4. Is the cover letter something a person would send under their own name?
  5. OQ-7: do the character counts suggest the document floors or ceilings are wrong?
OQ-8's revisit trigger: fabricated experience or unusable structure in more than 2 of 10 pairs is
the moment for an ADR proposing a different model, not a quiet swap."""

_MODEL_SWAP_NOTE: Final = """\
MODEL SWAP (required check 2): thinking is disabled by ThinkingConfig(thinking_budget=0) in
build_generate_config (infrastructure/llm/gemini.py). Gemini 3.5+ models reject thinking_budget -- it
is replaced by thinking_level -- so pointing GEMINI_MODEL at one of them without revisiting that
function turns every run into llm_error (a 400 the adapter has no better name for). Change the
config in the same commit as the model, and re-run this eval before merging."""


def _emit(out: TextIO, line: str = "") -> None:
    print(line, file=out, flush=True)


def _banner(out: TextIO, lines: Sequence[str]) -> None:
    rule = "!" * max(len(line) for line in lines)
    _emit(out, rule)
    for line in lines:
        _emit(out, line)
    _emit(out, rule)


def _count(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def render_corpus(
    out: TextIO, corpus: Corpus, pairs: Sequence[CorpusPair], *, settings: Settings
) -> None:
    """The corpus as the pipeline will see it: character counts after normalization, and the exact
    size of each assembled prompt (the real `build_tailoring_prompt`, no estimate)."""
    _emit(out, f"corpus: {corpus.root.resolve()}")
    _emit(
        out,
        f"  {len(corpus.cvs)} synthetic CVs, {len(corpus.postings)} synthetic postings, "
        f"{len(corpus.pairs)} pairs ({len(pairs)} selected)",
    )
    _emit(out)
    _emit(out, f"  {'pair':<6}{'cv':<28}{'cv chars':>10}  {'posting':<34}{'chars':>8}{'prompt':>9}")
    for pair in pairs:
        prompt_characters = len(build_tailoring_prompt(pair.cv.text.value, pair.posting.text.value))
        _emit(
            out,
            f"  {pair.id:<6}{pair.cv.id:<28}{pair.cv.text.character_count:>10,}  "
            f"{pair.posting.id:<34}{pair.posting.text.character_count:>8,}{prompt_characters:>9,}",
        )
    _emit(
        out,
        f"  (cv chars are checked against llm_max_cv_characters = "
        f"{settings.llm_max_cv_characters:,}; prompt is characters, not tokens)",
    )


def render_header(
    out: TextIO,
    corpus: Corpus,
    pairs: Sequence[CorpusPair],
    settings: Settings,
    *,
    thinking_budget: int | None,
) -> None:
    _emit(out, "=" * 100)
    _emit(
        out,
        "TailorCraft prompt eval (make eval) -- NOT a test. It calls the real API and costs money.",
    )
    _emit(out, "=" * 100)
    _emit(
        out,
        f"model: {settings.gemini_model}   prompt_version: {PROMPT_VERSION}   "
        f"thinking_budget sent: {thinking_budget}   max_output_tokens: "
        f"{settings.llm_max_output_tokens:,}",
    )
    _emit(
        out,
        f"at most {len(pairs) * max(1, settings.llm_max_attempts)} paid calls "
        f"({len(pairs)} pairs x llm_max_attempts={settings.llm_max_attempts}); runs are sequential, "
        "because concurrent calls would measure contention, not the budget",
    )
    render_corpus(out, corpus, pairs, settings=settings)
    _emit(out)
    _emit(out, _JUDGEMENT_GUIDE)
    _emit(out)


def _calls_line(calls: Sequence[SdkCall]) -> str:
    parts: list[str] = []
    for number, call in enumerate(calls, start=1):
        if call.error_type is not None:
            parts.append(f"#{number} {call.duration_ms:,} ms raised {call.error_type}")
        else:
            thoughts = "absent" if call.thoughts_tokens is None else f"{call.thoughts_tokens:,}"
            parts.append(f"#{number} {call.duration_ms:,} ms returned, thoughts {thoughts}")
    return "; ".join(parts) if parts else "none (refused before any call)"


def render_pair(out: TextIO, result: PairResult, *, width: int) -> None:
    pair = result.pair
    _emit(out, "#" * width)
    _emit(out, f"PAIR {pair.id} -- {pair.posting.title} at {pair.posting.company}")
    _emit(out, f"cv: {pair.cv.id}   posting: {pair.posting.id}")
    if pair.watch_for:
        for line in textwrap.wrap(f"watch for: {pair.watch_for}", width, subsequent_indent="  "):
            _emit(out, line)
    outcome = f"{result.outcome} ({result.problem})" if result.problem else result.outcome
    tokens = (
        f"prompt {result.draft.metrics.prompt_tokens:,} / completion "
        f"{result.draft.metrics.completion_tokens:,}"
        if result.draft is not None
        else "-"
    )
    _emit(
        out,
        f"outcome: {outcome}   llm_duration_ms: {_count(result.llm_duration_ms)}   "
        f"wall: {result.wall_ms:,} ms   tokens: {tokens}",
    )
    _emit(out, f"calls: {_calls_line(result.calls)}")
    _emit(out, "checks:")
    for check in result.checks:
        _emit(out, f"  [{check.status.value.upper():<4}] {check.name}: {check.detail}")
    if result.draft is None:
        _emit(out, "no documents to read: the run did not succeed.")
        _emit(out)
        return
    if result.unfamiliar_phrases:
        phrases = "; ".join(result.unfamiliar_phrases)
        for line in textwrap.wrap(
            f"unfamiliar phrases in the experience section (for your eye, not a check): {phrases}",
            width,
            subsequent_indent="  ",
        ):
            _emit(out, line)
    _emit(out)
    _side_by_side(
        out,
        "BASE CV (as committed; the model receives it whitespace-collapsed, as extraction yields)",
        pair.cv.raw_text,
        f"TAILORED CV ({len(result.draft.documents.cv.value):,} chars)",
        result.draft.documents.cv.value,
        width,
    )
    _emit(out)
    _side_by_side(
        out,
        "JOB POSTING (as committed)",
        pair.posting.raw_text,
        f"COVER LETTER ({len(result.draft.documents.cover_letter.value):,} chars)",
        result.draft.documents.cover_letter.value,
        width,
    )
    _emit(out)


def render_summary(
    out: TextIO, summary: Summary, settings: Settings, *, results: Sequence[PairResult]
) -> None:
    _emit(out, "=" * 100)
    _emit(out, "SUMMARY")
    _emit(out, "=" * 100)
    failed = ", ".join(f"{label}: {n}" for label, n in summary.failures) or "none"
    _emit(
        out,
        f"pairs: {summary.pair_count}   succeeded: {summary.succeeded_count}   failed: {failed}   "
        f"SDK calls made: {summary.call_count}",
    )
    _emit(out)
    _emit(
        out,
        f"  {'pair':<6}{'outcome':<40}{'llm_ms':>9}{'wall_ms':>9}{'calls':>6}"
        f"{'prompt_tok':>11}{'compl_tok':>10}  {'thoughts':<12}checks",
    )
    for result in results:
        outcome = f"{result.outcome} ({result.problem})" if result.problem else result.outcome
        thoughts = "/".join(
            "x"
            if c.error_type is not None
            else "-"
            if c.thoughts_tokens is None
            else str(c.thoughts_tokens)
            for c in result.calls
        )
        prompt_tokens = result.draft.metrics.prompt_tokens if result.draft is not None else None
        completion_tokens = (
            result.draft.metrics.completion_tokens if result.draft is not None else None
        )
        failed_checks = len(result.failed_checks)
        _emit(
            out,
            f"  {result.pair.id:<6}{outcome[:39]:<40}{_count(result.llm_duration_ms):>9}"
            f"{result.wall_ms:>9,}{len(result.calls):>6}{_count(prompt_tokens):>11}"
            f"{_count(completion_tokens):>10}  {thoughts or '-':<12}"
            f"{'pass' if not failed_checks else f'FAIL x{failed_checks}'}",
        )
    _emit(
        out, "  (thoughts per call: - absent, x the call raised, a number is thoughts_token_count)"
    )
    _emit(out)

    n = len(summary.llm_duration_ms)
    if n:
        _emit(
            out,
            f"llm_duration_ms over {n} succeeded run(s): p50 {_count(summary.llm_p50_ms)}   "
            f"p95 {_count(summary.verdict.p95_ms if summary.verdict else None)}   "
            f"min {min(summary.llm_duration_ms):,}   max {max(summary.llm_duration_ms):,}",
        )
        _emit(
            out,
            "  (nearest-rank percentiles: always an observed value; with fewer than 20 runs, p95 is "
            "the slowest run)",
        )
    _emit(
        out,
        f"wall clock per run, retries and backoff included: p50 {_count(summary.wall_p50_ms)}   "
        f"p95 {_count(summary.wall_p95_ms)}",
    )
    mean_prompt = summary.mean_prompt_tokens
    mean_completion = summary.mean_completion_tokens
    _emit(
        out,
        "mean tokens over succeeded runs: prompt "
        f"{'-' if mean_prompt is None else f'{mean_prompt:,.0f}'}   completion "
        f"{'-' if mean_completion is None else f'{mean_completion:,.0f}'}   "
        "(completion excludes thoughts, so it is what the model wrote, not what was billed)",
    )
    _emit(out)

    # Required check 1.
    observed = (
        summary.thoughts_absent_calls + summary.thoughts_zero_calls + len(summary.thoughts_nonzero)
    )
    if summary.thoughts_nonzero:
        listed = ", ".join(
            f"pair {pair_id}: {tokens:,}" for pair_id, tokens in summary.thoughts_nonzero
        )
        _banner(
            out,
            [
                "THINKING IS NOT OFF (required check 1): thoughts_token_count was non-zero on "
                f"{len(summary.thoughts_nonzero)} of {observed} returned call(s).",
                f"  {listed}",
                "thinking_budget=0 is not being honoured by this model. Thinking tokens count against",
                f"max_output_tokens ({settings.llm_max_output_tokens:,}) and against latency, so resolve "
                "this BEFORE the latency numbers above mean anything.",
            ],
        )
    elif observed:
        _emit(
            out,
            f"THINKING (required check 1): off. thoughts_token_count absent on "
            f"{summary.thoughts_absent_calls} and zero on {summary.thoughts_zero_calls} of {observed} "
            "returned call(s).",
        )
    else:
        _emit(out, "THINKING (required check 1): NOT OBSERVED -- no call returned a response.")
    if summary.unobserved_calls:
        _emit(
            out,
            f"  {summary.unobserved_calls} call(s) raised before returning, so their usage "
            "(thoughts included) could not be read.",
        )
    _emit(out)
    _emit(out, _MODEL_SWAP_NOTE)
    _emit(out)

    # The per-attempt bound is also the ceiling on any `llm_duration_ms` this sample can contain: a call
    # slower than it is not a long duration, it is an `llm_timed_out` run with no duration at all. So
    # a model too slow for the budget does NOT raise p95 — with the default 12 s bound, p95 + plumbing
    # tops out at 14 s and the arithmetic alone can only ever say "within". Timeouts are where an
    # over-budget model actually appears, which is why a WITHIN over a sample that excluded them is
    # reported as not shown rather than as a pass.
    attempt_ceiling_ms = round(settings.llm_request_timeout_seconds * 1000)
    verdict = summary.verdict
    if verdict is None:
        _banner(
            out,
            [
                "NO BUDGET VERDICT: no run succeeded, so there is no llm_duration_ms to measure.",
                "That is itself a finding, and the eval exits 1.",
            ],
        )
    elif not verdict.within:
        _banner(
            out,
            [
                f"OVER BUDGET: p95 llm_duration_ms {verdict.p95_ms:,} + plumbing bound "
                f"{verdict.plumbing_ms:,} = {verdict.total_ms:,} ms > {verdict.budget_ms:,} ms "
                f"(over by {-verdict.margin_ms:,} ms, n={verdict.sample_size}).",
                "AC-20(b): this is a finding to resolve BEFORE the slice closes -- trim the inputs,",
                "change the model, or amend Constitution section 7 on purpose. Never silently.",
            ],
        )
    elif summary.timed_out_count:
        _banner(
            out,
            [
                f"BUDGET NOT SHOWN: p95 llm_duration_ms {verdict.p95_ms:,} + plumbing bound "
                f"{verdict.plumbing_ms:,} = {verdict.total_ms:,} ms <= {verdict.budget_ms:,} ms, but "
                f"{summary.timed_out_count} of {summary.pair_count} run(s) failed as llm_timed_out "
                "and are NOT in that sample.",
                "A successful attempt cannot outlast llm_request_timeout_seconds "
                f"({attempt_ceiling_ms:,} ms), so a model too slow for the budget shows up as",
                "timeouts, never as a high p95. Every timed-out run missed the budget. AC-20(b): resolve",
                "this before the slice closes, exactly as for an over-budget p95. Never silently.",
            ],
        )
    else:
        _emit(
            out,
            f"BUDGET (Constitution section 7, AC-20(b)): WITHIN. p95 llm_duration_ms {verdict.p95_ms:,} "
            f"+ plumbing bound {verdict.plumbing_ms:,} = {verdict.total_ms:,} ms <= "
            f"{verdict.budget_ms:,} ms ({verdict.margin_ms:,} ms headroom, n={verdict.sample_size}); "
            "no run timed out.",
        )
    if verdict is not None and summary.succeeded_count < summary.pair_count:
        _emit(
            out,
            f"  The verdict covers {summary.succeeded_count} of {summary.pair_count} runs: a failed run "
            "has no llm_duration_ms.",
        )
    if summary.retried_success_count:
        _emit(
            out,
            f"  {summary.retried_success_count} succeeded run(s) needed a retry. Their llm_duration_ms "
            "is the successful attempt only; in wall time each has already missed the budget.",
        )
    _emit(
        out,
        f"  (no llm_duration_ms can exceed llm_request_timeout_seconds = {attempt_ceiling_ms:,} ms: "
        "a slower call is recorded as a timeout, not as a duration)",
    )
    _emit(
        out,
        f"  (plumbing bound {PLUMBING_BOUND_MS:,} ms is AC-20(a)'s asserted ceiling; budget "
        f"{CONSTITUTION_BUDGET_MS:,} ms is Constitution section 7)",
    )
    _emit(out)
    _emit(
        out,
        f"EXIT {summary.exit_code}: {summary.failed_check_count} failed check(s)"
        + ("" if verdict is None or verdict.within else ", over budget")
        + ("" if verdict is not None else ", no budget verdict")
        + ". Exit 0 would mean only that the cheap checks passed -- read the documents.",
    )


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        if not raw.strip():
            lines.append("")
            continue
        stripped = raw.lstrip()
        indent = raw[: min(len(raw) - len(stripped), _MAX_INDENT)]
        hanging = indent + ("  " if stripped[:2] in {"- ", "* ", "+ "} else "")
        lines.extend(
            textwrap.wrap(
                stripped,
                width,
                initial_indent=indent,
                subsequent_indent=hanging,
                break_on_hyphens=False,
            )
            or [""]
        )
    return lines


def _side_by_side(
    out: TextIO, left_title: str, left: str, right_title: str, right: str, width: int
) -> None:
    column = max(_MIN_COLUMN, (width - len(_GUTTER)) // 2)
    left_lines = [*_wrap(left_title, column), "-" * column, *_wrap(left, column)]
    right_lines = [*_wrap(right_title, column), "-" * column, *_wrap(right, column)]
    for left_line, right_line in zip_longest(left_lines, right_lines, fillvalue=""):
        _emit(out, f"{left_line:<{column}}{_GUTTER}{right_line}".rstrip())
