"""The prompt-eval runner: each corpus pair through the real `GeminiLlm`, recorded, checked, printed.

An entry point's worth of orchestration and no business rule — the rules it measures against live in
the value objects (`TailoredCv`, `CoverLetter`) and in the adapter it drives, and the eval re-uses
them rather than restating them.

**Never logs or prints a user's CV**, because it never sees one: its only input is the committed
synthetic corpus. It does print tailored documents — to a person, who is the point of the exercise.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final, TextIO

from tailorcraft.domain.tailoring.errors import LlmOutputInvalid, TailoringFailed
from tailorcraft.domain.tailoring.value_objects import TailoredDraft
from tailorcraft.infrastructure.llm.evaluation.checks import (
    experience_section,
    foreign_organisations,
    mentions,
    missing_entries,
    unfamiliar_phrases,
)
from tailorcraft.infrastructure.llm.evaluation.corpus import (
    Corpus,
    CorpusError,
    CorpusPair,
    load_corpus,
)
from tailorcraft.infrastructure.llm.evaluation.report import (
    DEFAULT_WIDTH,
    render_corpus,
    render_header,
    render_pair,
    render_summary,
)
from tailorcraft.infrastructure.llm.evaluation.results import (
    OUTCOME_SUCCEEDED,
    CheckResult,
    CheckStatus,
    PairResult,
    SdkCall,
    summarise,
)
from tailorcraft.infrastructure.llm.gemini import (
    GeminiLlm,
    GenerateFn,
    RawCompletion,
    build_generate_config,
)
from tailorcraft.infrastructure.llm.parsing import (
    PROBLEM_DOCUMENT_TOO_LONG,
    PROBLEM_DOCUMENT_TOO_SHORT,
)
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.settings import Settings, get_settings

# The parser's labels for an output the value objects refused on length — the only way a length
# check over this pipeline can fail (see `_lengths_in_bounds`).
_LENGTH_PROBLEMS: Final = frozenset({PROBLEM_DOCUMENT_TOO_SHORT, PROBLEM_DOCUMENT_TOO_LONG})

_CHECK_DOCUMENTS = "both documents present"
_CHECK_LENGTHS = "lengths within the value objects' bounds"
_CHECK_CV_EMPLOYERS = "tailored CV names no employer outside the base CV"
_CHECK_CV_ENTRIES = "tailored CV keeps every employer and education entry"
_CHECK_LETTER_EMPLOYERS = "cover letter names no other candidate's employer"
_CHECK_CV_NAME = "tailored CV names the candidate as the base CV does"
_CHECK_LETTER_NAME = "cover letter names the candidate as the base CV does"
_CHECK_THINKING = "thinking off (thoughts_token_count zero or absent)"


class RecordingGenerate:
    """A `GenerateFn` that forwards to another and records each call's duration and usage.

    Injected into `GeminiLlm` in place of its default, wrapping that default (`GeminiLlm.generate`), so
    the adapter's whole `tailor()` still runs — both timeouts, the retry loop, parsing, metrics — and
    every individual attempt is visible, not only the one that succeeded. That matters for check 1: a
    thinking model can spend its output budget on the FIRST attempt, fail as `not_json`, and succeed on
    the retry, and `LlmCallMetrics` only ever describes the survivor.

    Records numbers and exception type names only. The prompt passes through untouched and is never
    stored; the completion's text is never read.
    """

    __slots__ = ("_calls", "_inner")

    def __init__(self, inner: GenerateFn) -> None:
        self._inner = inner
        self._calls: list[SdkCall] = []

    @property
    def calls(self) -> tuple[SdkCall, ...]:
        return tuple(self._calls)

    async def __call__(self, prompt: str) -> RawCompletion:
        started_at = time.perf_counter()
        try:
            completion = await self._inner(prompt)
        except BaseException as exc:
            # BaseException, and re-raised untouched: the adapter's per-attempt `wait_for` cancels this
            # coroutine on timeout, and that cancellation must reach it exactly as if we were not here.
            self._calls.append(
                SdkCall(
                    duration_ms=_elapsed_ms(started_at),
                    error_type=_qualified_type(exc),
                    prompt_tokens=None,
                    completion_tokens=None,
                    thoughts_tokens=None,
                )
            )
            raise
        self._calls.append(
            SdkCall(
                duration_ms=_elapsed_ms(started_at),
                error_type=None,
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
                thoughts_tokens=completion.thoughts_tokens,
            )
        )
        return completion


async def evaluate_pair(
    pair: CorpusPair,
    *,
    settings: Settings,
    generate: GenerateFn,
    corpus_organisations: Sequence[str],
) -> PairResult:
    """One pair through the real pipeline, then the cheap checks over whatever came back."""
    recorder = RecordingGenerate(generate)
    adapter = GeminiLlm(settings, generate=recorder)
    started_at = time.perf_counter()
    draft: TailoredDraft | None = None
    outcome = OUTCOME_SUCCEEDED
    problem: str | None = None
    try:
        draft = await adapter.tailor(pair.cv.text, pair.posting.text)
    except TailoringFailed as failure:
        outcome = failure.reason.value
        if isinstance(failure, LlmOutputInvalid):
            problem = failure.problem
    except Exception as exc:
        # `LlmPort` promises a `TailoringFailed` on every failure; anything else escaping is a breach
        # of that contract. Recorded as a finding rather than raised, so the paid results of the pairs
        # already run are still reported — this is a measurement, and losing nine answers to report
        # one crash would be the expensive way to learn about it.
        outcome = f"port_contract_breach:{_qualified_type(exc)}"
    wall_ms = _elapsed_ms(started_at)
    calls = recorder.calls
    checks = (
        _documents_present(draft, outcome, problem),
        _lengths_in_bounds(draft, problem),
        _cv_names_no_foreign_employer(pair, draft, corpus_organisations),
        _cv_keeps_every_entry(pair, draft),
        _letter_names_no_other_candidates_employer(pair, draft, corpus_organisations),
        _cv_names_candidate(pair, draft),
        _letter_names_candidate(pair, draft),
        _thinking_off(calls),
    )
    phrases = (
        unfamiliar_phrases(experience_section(draft.documents.cv.value).text, pair.cv.raw_text)
        if draft is not None
        else ()
    )
    return PairResult(
        pair=pair,
        outcome=outcome,
        problem=problem,
        draft=draft,
        calls=calls,
        wall_ms=wall_ms,
        checks=checks,
        unfamiliar_phrases=phrases,
    )


async def run_eval(
    corpus: Corpus,
    settings: Settings,
    *,
    out: TextIO,
    pairs: Sequence[CorpusPair] | None = None,
    width: int = DEFAULT_WIDTH,
    generate: GenerateFn | None = None,
) -> int:
    """Run `pairs` (default: the whole corpus), print the report to `out`, return the exit code.

    `generate` is the testing seam, with **the real SDK path as its strict default** — the same shape
    as `GeminiLlm`'s own. `make eval` never passes it; an offline verification passes a fake.
    """
    selected = tuple(pairs) if pairs is not None else corpus.pairs
    # A `GeminiLlm` built with no injected seam, so `.generate` is `_generate_with_sdk` — the exact
    # path production binds. Built once, so one SDK client serves every pair.
    base = generate if generate is not None else GeminiLlm(settings).generate
    thinking_budget = _thinking_budget_sent(settings)
    render_header(out, corpus, selected, settings, thinking_budget=thinking_budget)
    results: list[PairResult] = []
    for pair in selected:
        # Sequential on purpose: concurrent calls would measure contention and the provider's rate
        # limiting, not the per-run latency the 15-second budget is about.
        result = await evaluate_pair(
            pair,
            settings=settings,
            generate=base,
            corpus_organisations=corpus.all_organisations,
        )
        render_pair(out, result, width=width)
        results.append(result)
    summary = summarise(results)
    render_summary(out, summary, settings, results=results)
    return summary.exit_code


def run_from_cli(
    *, corpus_dir: Path, only: Sequence[str] | None, width: int, validate_only: bool
) -> int:
    """`tailorcraft.cli eval-prompts`. Exit 0/1 from the eval; 2 when it cannot start.

    The corpus is validated before the key is looked at, so `--validate-only` works on a machine with
    no key — and a broken corpus is reported before anything can be spent. Sentry is deliberately not
    initialised: this is an operator at a terminal, not a service.
    """
    settings = get_settings()
    configure_logging(settings)
    _route_logs_to_stderr()
    try:
        corpus = load_corpus(corpus_dir, max_cv_characters=settings.llm_max_cv_characters)
        pairs = corpus.select(only)
    except CorpusError as error:
        print(f"eval-prompts: {error}", file=sys.stderr)
        return 2
    if validate_only:
        render_corpus(sys.stdout, corpus, pairs, settings=settings)
        print("\ncorpus is valid; no API call was made.")
        return 0
    if not settings.gemini_api_key.strip():
        print(
            "eval-prompts: GEMINI_API_KEY is empty, so every run would fail as llm_unavailable "
            "without a call. Set it in the root .env and re-run, or use --validate-only.",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(run_eval(corpus, settings, out=sys.stdout, pairs=pairs, width=width))


# -- checks ----------------------------------------------------------------------------------------


def _documents_present(
    draft: TailoredDraft | None, outcome: str, problem: str | None
) -> CheckResult:
    if draft is not None:
        # `TailoredDocuments` makes "one document and not the other" unrepresentable (TR-5), so
        # a draft existing IS the check.
        return CheckResult(_CHECK_DOCUMENTS, CheckStatus.PASS, "tailored CV and cover letter")
    reason = f"{outcome} ({problem})" if problem else outcome
    return CheckResult(_CHECK_DOCUMENTS, CheckStatus.FAIL, f"the run failed as {reason}")


def _lengths_in_bounds(draft: TailoredDraft | None, problem: str | None) -> CheckResult:
    """The value objects' bounds, observed where they can actually be missed.

    **Not a re-application of `TailoredCv` and `CoverLetter` to the draft**, which is what this
    check first did, and which could never fail: a `TailoredDraft` is built from instances of those
    two types, so a draft that exists is inside their bounds by construction, and a check that cannot
    fail is decoration. An output outside the bounds never becomes a draft at all — the parser refuses
    it as `llm_output_invalid` with a length `problem` — so THAT is the failing branch, and it is a
    FAIL here rather than the SKIP "no documents" would suggest. The bounds stay the value objects'
    own; no second "400 characters" is written here to drift (see `parsing.py`).

    On a pass the detail prints the two quantities the bounds judge — non-whitespace characters for
    the floors, normalized length for the ceilings — which is the evidence OQ-5 asks this corpus for.
    """
    if problem in _LENGTH_PROBLEMS:
        return CheckResult(
            _CHECK_LENGTHS,
            CheckStatus.FAIL,
            f"the value objects refused the model's output as {problem}",
        )
    if draft is None:
        return CheckResult(_CHECK_LENGTHS, CheckStatus.SKIP, "no documents")
    cv = draft.documents.cv.value
    letter = draft.documents.cover_letter.value
    return CheckResult(
        _CHECK_LENGTHS,
        CheckStatus.PASS,
        f"CV {len(cv):,} chars ({_non_whitespace(cv):,} non-whitespace), "
        f"cover letter {len(letter):,} chars ({_non_whitespace(letter):,} non-whitespace)",
    )


def _cv_names_no_foreign_employer(
    pair: CorpusPair, draft: TailoredDraft | None, corpus_organisations: Sequence[str]
) -> CheckResult:
    if draft is None:
        return CheckResult(_CHECK_CV_EMPLOYERS, CheckStatus.SKIP, "no documents")
    cv = draft.documents.cv.value
    candidates = (*corpus_organisations, *pair.posting.names)
    allowed = pair.cv.organisations
    scope = experience_section(cv)
    where = "experience section" if scope.located else "whole CV (no experience heading found)"
    in_scope = foreign_organisations(scope.text, candidates=candidates, allowed=allowed)
    if in_scope:
        return CheckResult(
            _CHECK_CV_EMPLOYERS,
            CheckStatus.FAIL,
            f"{where} names {', '.join(in_scope)}, not in the base CV",
        )
    elsewhere = tuple(
        name
        for name in foreign_organisations(cv, candidates=candidates, allowed=allowed)
        if name not in in_scope
    )
    note = (
        f"; named elsewhere in the CV (read it, not failed): {', '.join(elsewhere)}"
        if elsewhere
        else ""
    )
    return CheckResult(_CHECK_CV_EMPLOYERS, CheckStatus.PASS, f"none in the {where}{note}")


def _cv_keeps_every_entry(pair: CorpusPair, draft: TailoredDraft | None) -> CheckResult:
    """No employer or education entry deleted. Limits on `checks.missing_entries`: presence, not
    placement, and nothing about titles or dates."""
    if draft is None:
        return CheckResult(_CHECK_CV_ENTRIES, CheckStatus.SKIP, "no documents")
    missing = missing_entries(draft.documents.cv.value, pair.cv.must_keep)
    if missing:
        return CheckResult(
            _CHECK_CV_ENTRIES,
            CheckStatus.FAIL,
            f"no accepted form of {', '.join(missing)} appears (whole phrase, any case) -- "
            "deleted rather than cut to one line, or renamed; read the experience and education",
        )
    kept = ", ".join(forms[0] for forms in pair.cv.must_keep)
    return CheckResult(
        _CHECK_CV_ENTRIES, CheckStatus.PASS, f"all {len(pair.cv.must_keep)} named: {kept}"
    )


def _letter_names_no_other_candidates_employer(
    pair: CorpusPair, draft: TailoredDraft | None, corpus_organisations: Sequence[str]
) -> CheckResult:
    """The posting's company belongs in a cover letter, so only other CVs' names are candidates."""
    if draft is None:
        return CheckResult(_CHECK_LETTER_EMPLOYERS, CheckStatus.SKIP, "no documents")
    found = foreign_organisations(
        draft.documents.cover_letter.value,
        candidates=corpus_organisations,
        allowed=pair.cv.organisations,
    )
    if found:
        return CheckResult(_CHECK_LETTER_EMPLOYERS, CheckStatus.FAIL, f"names {', '.join(found)}")
    return CheckResult(_CHECK_LETTER_EMPLOYERS, CheckStatus.PASS, "none")


def _cv_names_candidate(pair: CorpusPair, draft: TailoredDraft | None) -> CheckResult:
    if draft is None:
        return CheckResult(_CHECK_CV_NAME, CheckStatus.SKIP, "no documents")
    return _names_candidate(
        _CHECK_CV_NAME, "the tailored CV", draft.documents.cv.value, pair.cv.candidate_name
    )


def _letter_names_candidate(pair: CorpusPair, draft: TailoredDraft | None) -> CheckResult:
    """A letter signed with a misspelled name is as unsendable as a misspelled CV header."""
    if draft is None:
        return CheckResult(_CHECK_LETTER_NAME, CheckStatus.SKIP, "no documents")
    return _names_candidate(
        _CHECK_LETTER_NAME,
        "the cover letter",
        draft.documents.cover_letter.value,
        pair.cv.candidate_name,
    )


def _names_candidate(check: str, document_label: str, document: str, name: str) -> CheckResult:
    """Name fidelity: `document` contains the candidate's declared name (`corpus.toml`).

    Found in pair 05 of the second paid eval, by a person reading: the tailored CV headed the
    candidate "TOMAZ REYES, RN" where the base CV says "TOMASZ". The prompt already requires the name
    exactly as the CV states it, so this was a model slip — and no cheap check looked at the name.

    Matched with `mentions`, exactly as the employer check matches: whole phrase, case-insensitive,
    tolerant of line breaks and repeated whitespace. "Tomasz Reyes" and "TOMASZ REYES" pass; "TOMAZ
    REYES" fails. One check per document, so a FAIL line says which document lacks the name.

    **What it cannot see, stated plainly:**

    - It proves the name is **present somewhere** in the document, not that the header (or the
      letter's sign-off) carries it. A misspelled header over a correctly spelled name further down
      passes.
    - It does **not** catch an added middle name or a nickname once the declared form appears anywhere
      in the same document. "Tomasz J. Reyes" as the only form of the name fails (the phrase is
      broken), but a letter that opens "I am Tomasz Reyes" and is signed "Tom", or a CV headed
      "TOMASZ J. REYES" that names "Tomasz Reyes" further down, passes.
    - A person referred to **only by initials** ("T. Reyes") fails here, correctly for this corpus,
      but a candidate whose own CV uses initials would need a different rule, not a different
      declaration.
    - A name split by Markdown markup inside it ("**Tomasz** Reyes") is missed, as for employers.
    """
    if mentions(document, name):
        return CheckResult(check, CheckStatus.PASS, f"names {name}")
    return CheckResult(
        check,
        CheckStatus.FAIL,
        f"{document_label} never names {name} as a whole phrase (any case) -- misspelled, "
        "shortened or missing; read its header and sign-off",
    )


def _thinking_off(calls: Sequence[SdkCall]) -> CheckResult:
    """Required check 1: every call that returned reported zero or no thoughts."""
    returned = [call for call in calls if call.error_type is None]
    if not returned:
        return CheckResult(
            _CHECK_THINKING, CheckStatus.SKIP, "no call returned a response, so no usage was read"
        )
    counts = ["absent" if c.thoughts_tokens is None else f"{c.thoughts_tokens:,}" for c in returned]
    if any(call.thoughts_tokens for call in returned):
        return CheckResult(
            _CHECK_THINKING,
            CheckStatus.FAIL,
            f"thoughts_token_count per returned call: {', '.join(counts)} -- thinking_budget=0 "
            "was NOT honoured",
        )
    return CheckResult(
        _CHECK_THINKING, CheckStatus.PASS, f"thoughts_token_count per call: {', '.join(counts)}"
    )


# -- helpers ---------------------------------------------------------------------------------------


def _thinking_budget_sent(settings: Settings) -> int | None:
    """Read back from the config the real call sends, rather than restating what we believe it is."""
    thinking = build_generate_config(settings).thinking_config
    return thinking.thinking_budget if thinking is not None else None


def _non_whitespace(text: str) -> int:
    """The quantity the value objects' floors count, measured the same way they measure it."""
    return sum(1 for char in text if not char.isspace())


def _route_logs_to_stderr() -> None:
    """`configure_logging` writes to stdout, which is right for a container and wrong here: the report
    is stdout, and `make eval > report.txt` should capture the report and nothing else."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(sys.stderr)


def _qualified_type(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"


def _elapsed_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)
