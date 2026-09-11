"""`GeminiLlm` — the `LlmPort` adapter, and **the only file in this repository that imports the
Gemini SDK.**

That is a gate, not a convention: `google` is in the forbidden module list of *both* the domain and
the application import-linter contracts, as the bare namespace package rather than `google.genai`
(the narrower string is rejected outright by import-linter). If a second module under `api/src/` ever
imports it, the port has stopped being a boundary and become a rename — `LlmPort` mentions no Gemini,
no HTTP, no JSON, no timeout and no retry count precisely so that swapping the provider is a change
to one file rather than to the business model (ADR-0004).

**This is not a guarded egress — the host is fixed and first-party, so ADR-0012 does not attach.** A
reader arriving from `infrastructure/posting/fetching.py` will otherwise look for an address policy
and either add one or wonder who deleted it. ADR-0012's ten obligations exist because that adapter
fetches *a URL a stranger chose*; here the destination is Google, configured by us, reached through a
vendor SDK. What the two adapters DO share is the shape of everything else: a constructor-argument
testing seam with a strict default, layered timeouts under one hard outer bound, and an
`except Exception` floor that makes the port's promise true by construction rather than by
enumeration.

**Nothing from the prompt or the completion is logged, at any step, at any level.** The prompt is the
user's entire CV — name, address, phone number, employment history — handed over by somebody who is
unemployed and in a hurry (Constitution §8). Three events leave this module: `llm.call_started`,
`llm.call_succeeded` and `llm.call_failed`, plus one line each for the two pre-flight refusals. Every
one of them carries ids, counts, durations, token counts and fixed labels. A provider's exception
*message* is treated as radioactive — it can quote the request body straight back — so the floor logs
`error_type` and never `str(exc)`, and never `exc_info`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import httpx
import structlog
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from tailorcraft.domain.intake.value_objects import ExtractedText
from tailorcraft.domain.posting.value_objects import JobPostingText
from tailorcraft.domain.tailoring.errors import (
    LlmError,
    LlmInputsTooLarge,
    LlmOutputInvalid,
    LlmRateLimited,
    LlmRefused,
    LlmTimedOut,
    LlmUnavailable,
    TailoringFailed,
)
from tailorcraft.domain.tailoring.value_objects import (
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredDraft,
)
from tailorcraft.infrastructure.llm.parsing import (
    KEY_COVER_LETTER,
    KEY_TAILORED_CV,
    PROBLEM_DOCUMENT_TOO_LONG,
    parse_tailoring_response,
)
from tailorcraft.infrastructure.llm.prompt import PROMPT_VERSION, build_tailoring_prompt
from tailorcraft.infrastructure.settings import Settings

if TYPE_CHECKING:
    from tailorcraft.domain.tailoring.ports import LlmPort

log = structlog.get_logger(__name__)


# The two failures that are decided before a single byte leaves the process. Both make ZERO API
# calls, and both are logged with `llm_call_skipped=True` so "runs that cost us nothing" is one
# filter rather than a join across two event names.
_EVENT_NOT_CONFIGURED: Final = "llm.not_configured"
_EVENT_INPUTS_TOO_LARGE: Final = "llm.inputs_too_large"

_EVENT_STARTED: Final = "llm.call_started"
_EVENT_SUCCEEDED: Final = "llm.call_succeeded"
_EVENT_FAILED: Final = "llm.call_failed"

# The four classes a second attempt could plausibly change the answer for. **`LlmRefused` is not
# here, and that is the whole point of the tuple existing**: a refusal retried is a refusal repeated
# at twice the price (the `gemini-tailoring` skill is explicit about it). Neither is
# `LlmInputsTooLarge` — nothing about the second attempt would differ, and it never reaches the retry
# loop anyway because it is raised pre-flight.
_RETRYABLE: Final = (LlmUnavailable, LlmRateLimited, LlmTimedOut, LlmOutputInvalid)

# The response schema (technical plan, step 4). Built from `parsing.py`'s key constants rather than
# from string literals, so the schema we ask for and the keys the parser demands **cannot drift** —
# a renamed field would otherwise surface as a `missing_field` nobody can explain.
#
# Asking for a schema is a HINT to a probabilistic system, not a guarantee from a compiler, which is
# why `parse_tailoring_response` runs on every response regardless of what this asked for.
_RESPONSE_SCHEMA: Final = genai_types.Schema(
    type=genai_types.Type.OBJECT,
    properties={
        KEY_TAILORED_CV: genai_types.Schema(type=genai_types.Type.STRING),
        KEY_COVER_LETTER: genai_types.Schema(type=genai_types.Type.STRING),
    },
    required=[KEY_TAILORED_CV, KEY_COVER_LETTER],
)

# The `finish_reason`s that mean *the model declined*, as opposed to *the model stopped*. Checked
# against the installed SDK's enum (`google.genai.types.FinishReason`) rather than guessed, and
# deliberately partial: `MAX_TOKENS` is NOT here, because a truncated answer is not a refusal — it
# reaches `parse_tailoring_response` as unparseable JSON, is labelled `not_json`, and is retried
# once. Relabelling it as a refusal would suppress the retry and hide the real cause, which is
# `llm_max_output_tokens`.
_REFUSAL_FINISH_REASONS: Final = frozenset(
    {
        genai_types.FinishReason.SAFETY,
        genai_types.FinishReason.PROHIBITED_CONTENT,
        genai_types.FinishReason.BLOCKLIST,
        genai_types.FinishReason.SPII,
        genai_types.FinishReason.RECITATION,
    }
)


@dataclass(frozen=True, slots=True)
class RawCompletion:
    """What crosses the SDK seam: the model's raw text and what the call cost in tokens.

    Deliberately three plain fields and **not** a `GenerateContentResponse`. The seam exists so the
    adapter's own tests can drive every path in this module with no network and no SDK objects to
    construct; a seam typed as the vendor's response object would make a stub a small reimplementation
    of the vendor's model, which is the point at which a fake stops proving anything.

    `text` is the response verbatim — possibly fenced, possibly wrapped in prose, possibly truncated.
    It is **never logged and never put in an exception message** (Constitution §8): it is half a CV.
    """

    text: str
    prompt_tokens: int
    completion_tokens: int


# A prompt string in, raw text plus usage counts out. The narrowest signature that lets the whole of
# `tailor()` — the retry loop, the timeouts, the translations, the metrics, the logging — be exercised
# without a network, and narrow on purpose: a seam that accepted a `TailoringRun`, a settings object
# or a session id would be one refactor away from putting one of them in the prompt (AC-24).
GenerateFn = Callable[[str], Awaitable[RawCompletion]]


class _AttemptCounter:
    """Which attempt is in flight, readable from outside the retry loop.

    A mutable box rather than a return value, because the one caller who needs it — the outer
    deadline's handler — only ever runs when `_attempt_all` did *not* return. It exists so one log
    field can be a fact instead of an inference.
    """

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0


def raw_completion_from_response(response: genai_types.GenerateContentResponse) -> RawCompletion:
    """Translate one SDK response into a `RawCompletion`, or raise `LlmRefused`.

    Module-level and public rather than a private method, because it is the one piece of step 9's
    translation that is **response-shaped instead of exception-shaped**: a safety block does not
    arrive as an exception, it arrives as a perfectly successful HTTP 200 carrying no text. Keeping
    it here as a pure function is what lets T34 table-test the refusal rows against real SDK response
    objects without a network — the same reason `parse_tailoring_response` is pure.

    Raises:
        LlmRefused: the prompt was blocked, or the only candidate stopped for a safety-class reason.
    """
    feedback = response.prompt_feedback
    if feedback is not None and feedback.block_reason is not None:
        # The prompt itself was blocked. **The block reason's text is never logged and never
        # returned** (G-18): it can quote the content that triggered it.
        raise LlmRefused()

    candidates = response.candidates or []
    for candidate in candidates:
        if candidate.finish_reason in _REFUSAL_FINISH_REASONS:
            raise LlmRefused()

    # An empty candidate list with NO block reason is deliberately not a refusal. It is an
    # unusable response, which `parse_tailoring_response` will label `not_json` — and that is
    # retryable, where a refusal is not. Guessing "refused" here would turn a transient provider
    # oddity into a dead end with no retry and a "the model declined" message the user cannot act on.
    text = response.text or ""

    # Missing usage metadata records zeros rather than failing the run. A token count we did not get
    # is a gap in ACCOUNTING; the two documents in hand are still two good documents, and
    # `LlmCallMetrics` documents zero as legal for exactly this case. Failing here would throw away a
    # paid-for answer to protect a dashboard.
    usage = response.usage_metadata
    prompt_tokens = (usage.prompt_token_count or 0) if usage is not None else 0
    # `candidates_token_count` is the ANSWER's tokens. On a thinking model it excludes
    # `thoughts_token_count`, so this number is what the model wrote, not what the call was billed
    # for — stated here because the two diverge silently and `make eval`'s mean-token figures are
    # read as cost.
    completion_tokens = (usage.candidates_token_count or 0) if usage is not None else 0

    return RawCompletion(
        text=text, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )


class GeminiLlm:
    """Turn a CV and a job posting into a tailored draft, or into a failure the domain has a name for.

    `generate` is a constructor argument with the **real SDK client as the strict default**, exactly
    the seam `HttpxTrafilaturaFetcher` uses for its address policy. Nothing under `api/src/` ever
    constructs a stub — the stub lives only in T34's test module, and a wiring test asserts the
    production binding builds the real client. A seam that production could take by accident is not a
    seam, it is a bug with a docstring.

    **The no-key refusal is checked even when a stub is injected**, and that is a decision rather than
    an oversight. The alternative — "the key only matters on the real path" — would put the test and
    the production paths on different branches of the very check G-32 is about, and T34's "no-key path
    makes zero calls" test measures exactly that: an injected stub whose call count stays at zero. A
    test that drives the stub therefore supplies a placeholder key, which is one visible line in a
    fixture instead of an invisible exemption in the adapter.
    """

    def __init__(self, settings: Settings, generate: GenerateFn | None = None) -> None:
        self._settings = settings
        self._generate: GenerateFn = generate if generate is not None else self._generate_with_sdk
        # Built on first use, not here. With an empty key in dev the client is never constructed at
        # all, so nothing has to have an opinion about what `genai.Client(api_key="")` does.
        self._client: genai.Client | None = None

    async def tailor(self, cv: ExtractedText, posting: JobPostingText) -> TailoredDraft:
        """One tailoring call: refuse early, assemble, attempt, parse, measure.

        Raises:
            TailoringFailed: always a subclass of it, never a bare SDK, transport or parser
                exception — guaranteed structurally by the floor in `_attempt_once`, not by an
                allow-list of the vendor errors we happened to think of.
        """
        # Step 1 (G-22) and step 2 (G-32), in the technical plan's order, both **before any API
        # call** and both outside the retry loop — which is what makes each of them exactly one log
        # line rather than one per attempt.
        self._refuse_if_cv_is_too_large(cv)
        self._refuse_if_not_configured()

        log.info(
            _EVENT_STARTED,
            prompt_version=PROMPT_VERSION,
            model=self._settings.gemini_model,
            cv_character_count=cv.character_count,
            posting_character_count=posting.character_count,
        )

        # Step 3. The prompt is built here and never leaves this frame except as an argument to the
        # generate function. It carries the CV text and the posting text and nothing else (AC-24).
        prompt = build_tailoring_prompt(cv.value, posting.value)

        # The attempt in flight when the outer deadline fires. A counter rather than "assume it was
        # the last one": a deadline of 25 s with a 12 s per-attempt bound normally fires during the
        # SECOND attempt, but a deadline shortened in configuration fires during the first, and a log
        # line that guesses `attempt=2` for a run that made one call is telemetry that is wrong
        # exactly when somebody is reading it to find out what happened.
        counter = _AttemptCounter()
        started_at = time.perf_counter()

        # Step 6's outer half: ONE hard bound over the whole operation, retries and backoff included
        # — the layered shape of ADR-0012 obligation 8. The per-attempt timeout below is what a
        # normally-slow provider trips; this is what stops a provider that keeps the connection alive
        # and answers just often enough to reset it. Without it, `llm_max_attempts` bounds the number
        # of ways to be slow and not the total time, and a worker thread is held with a database
        # transaction open behind it.
        try:
            return await asyncio.wait_for(
                self._attempt_all(prompt, counter),
                timeout=self._settings.llm_total_deadline_seconds,
            )
        except TimeoutError:
            # The inner attempts have already logged their own `llm.call_failed`; this line is the
            # deadline itself firing, which is a different fact and deserves to be countable
            # separately from a per-attempt timeout. `duration_ms` is MEASURED rather than reported
            # as the deadline — `wait_for` fires at or after its timeout, and the difference is the
            # only evidence of a loop that was too busy to notice.
            log.warning(
                _EVENT_FAILED,
                failure_reason="llm_timed_out",
                attempt=counter.value,
                error_type="builtins.TimeoutError",
                duration_ms=_elapsed_ms(started_at),
                deadline_seconds=self._settings.llm_total_deadline_seconds,
                deadline="total",
            )
            # `from None` here as everywhere in this module: the cancelled inner coroutine's frames
            # hold the prompt, and a chained exception keeps them reachable from a Sentry report.
            raise LlmTimedOut() from None

    # -- the two pre-flight refusals -------------------------------------------------------------

    def _refuse_if_cv_is_too_large(self, cv: ExtractedText) -> None:
        """G-22. **Reject, never truncate** — the 1.2 rule, carried, and for the same reason: a run
        against a CV the user did not know was cut is a wrong answer they cannot diagnose.

        The posting needs no check here; `JobPostingText` already caps it at 30,000 characters in the
        domain, because "a posting longer than this is not a posting" is a rule about the type. "How
        much of a CV fits in one call" is not — it is a fact about *this model*, it moves when the
        model does, and that is precisely what an adapter's configuration is for.
        """
        limit = self._settings.llm_max_cv_characters
        if cv.character_count <= limit:
            return
        log.info(
            _EVENT_INPUTS_TOO_LARGE,
            base_cv_character_count=cv.character_count,
            limit=limit,
            llm_call_skipped=True,
        )
        raise LlmInputsTooLarge()

    def _refuse_if_not_configured(self) -> None:
        """G-32. No key, no call — and in production the process never reaches this line, because
        `Settings` refused to boot (AC-32).

        So this is the dev and test path, where an empty key is legal on purpose: it is what lets the
        whole suite run with no key and no possibility of a suite that starts spending money the day
        a secret appears. One log line, one `LlmUnavailable`, zero API calls.

        `LlmUnavailable` rather than a new reason of its own: from the user's side "we could not
        reach the model" is exactly what happened, and inventing a `not_configured` failure reason
        would add a row to the domain's vocabulary that only ever appears on a developer's laptop.
        """
        if self._settings.gemini_api_key.strip():
            return
        log.warning(_EVENT_NOT_CONFIGURED, model=self._settings.gemini_model, llm_call_skipped=True)
        raise LlmUnavailable()

    # -- attempts --------------------------------------------------------------------------------

    async def _attempt_all(self, prompt: str, counter: _AttemptCounter) -> TailoredDraft:
        """The bounded, class-aware retry (step 6's inner half).

        At most `llm_max_attempts` attempts — total, not extra — with `llm_retry_backoff_seconds`
        between them, and only for `_RETRYABLE`. There is exactly **one** retry mechanism in this
        slice: the Celery task declares no `autoretry_for`, no `retry_backoff` and no `max_retries`
        (AC-9), because two retry layers multiply a bounded cost by an unbounded one and double-spend
        on a run already recorded as failed.
        """
        attempts = max(1, self._settings.llm_max_attempts)
        for attempt in range(1, attempts + 1):
            counter.value = attempt
            try:
                return await self._attempt_once(prompt, attempt)
            except TailoringFailed as failure:
                if attempt >= attempts or not isinstance(failure, _RETRYABLE):
                    raise
                # Never retry without backoff. It sits INSIDE the outer `wait_for`, so a run that
                # spends its deadline sleeping is still cut off at the deadline.
                await asyncio.sleep(self._settings.llm_retry_backoff_seconds)
        # Unreachable: the loop either returns or raises on its final attempt. Kept so the function
        # has no implicit `None` return for mypy to infer, the same shape `_fetch_guarded` uses.
        raise LlmError()

    async def _attempt_once(self, prompt: str, attempt: int) -> TailoredDraft:
        """One call to the provider, translated. Every `except` below is step 9 or step 10.

        The specific translations carry the better reason and run first; the floor underneath makes
        `LlmPort`'s promise true by construction. "Better" is concrete rather than tidy: every branch
        here ends as a recorded `failure_reason` the client branches on, and `llm_rate_limited` (retry
        with backoff, the provider is busy) and `llm_error` (something broke inside us) are different
        things to tell a user who is waiting.
        """
        started_at = time.perf_counter()
        try:
            # Step 5 — the per-attempt bound, an `asyncio.wait_for` around the SDK call rather than a
            # client-level option we have not measured. The SDK's own `http_options.timeout` may well
            # work; this one is ours, is visible at the call site, and applies identically to the
            # injected stub, which is what makes the timeout path testable at all.
            completion = await asyncio.wait_for(
                self._generate(prompt), timeout=self._settings.llm_request_timeout_seconds
            )
        except TimeoutError:
            self._log_failure(
                "llm_timed_out",
                attempt,
                "builtins.TimeoutError",
                started_at,
                deadline_seconds=self._settings.llm_request_timeout_seconds,
                deadline="per_attempt",
            )
            raise LlmTimedOut() from None
        except TailoringFailed as failure:
            # Already in the domain's vocabulary: `LlmRefused` from `raw_completion_from_response`,
            # or whatever a stub raised. Logged here rather than at each raise site so every failure
            # leaving this method has produced exactly one `llm.call_failed` line.
            self._log_failure(failure.reason.value, attempt, _qualified_type(failure), started_at)
            raise
        except genai_errors.APIError as exc:
            # The SDK's own error tree: `ClientError` (4xx) and `ServerError` (5xx), both carrying a
            # numeric `code` and a gRPC-style `status`. Classified rather than caught per subclass,
            # because the interesting distinctions (429, UNAVAILABLE) cut across that split.
            translated = _classify_api_error(exc)
            if translated is None:
                self._log_floor(attempt, exc, started_at)
                raise LlmError() from None
            self._log_failure(translated.reason.value, attempt, _qualified_type(exc), started_at)
            raise translated from None
        except httpx.TimeoutException:
            # A per-phase transport timeout — connect, read, write or pool. **Not a subclass of the
            # builtin `TimeoutError`**, so the handler above does not see it; slice 1.2 shipped this
            # exact gap in the fetcher and it was invisible because the only timeout test used a slow
            # server, which the generous outer bound caught first. Written down here before it can be
            # rediscovered.
            self._log_failure(
                "llm_timed_out",
                attempt,
                "httpx.TimeoutException",
                started_at,
                deadline_seconds=self._settings.llm_request_timeout_seconds,
                deadline="transport",
            )
            raise LlmTimedOut() from None
        except (httpx.TransportError, ConnectionError) as exc:
            # The connection never established, or died mid-response: DNS, refused, reset, a TLS
            # handshake failure, a broken protocol. The SDK's async path lets httpx's transport
            # exceptions through untouched (measured against the installed google-genai, not
            # assumed), and the builtin `ConnectionError` is included so a future transport swap
            # still lands on the right reason rather than on the floor.
            self._log_failure("llm_unavailable", attempt, _qualified_type(exc), started_at)
            raise LlmUnavailable() from None
        except Exception as exc:
            # **THE FLOOR** (step 10), load-bearing rather than defensive habit. `LlmPort.tailor`
            # promises a `TailoringFailed` subclass on EVERY failure, and the surface underneath is
            # the SDK's tree plus httpx's plus pydantic's plus whatever a transitive dependency
            # raises. An allow-list is a bet that you enumerated all of it, and CLAUDE.md records
            # losing that bet in the extraction sweep, where `KeyError`, `AttributeError`,
            # `ValueError` and `LimitReachedError` all escaped a port that promised to translate
            # every failure.
            #
            # `Exception`, never `BaseException` — `asyncio.CancelledError` must still cancel a run
            # (G-24). A worker shutting down is not a tailoring failure.
            self._log_floor(attempt, exc, started_at)
            raise LlmError() from None

        # Deliberately outside the timed block: `duration_ms` is the MODEL's share of the 15-second
        # budget (the technical plan's budget table attributes it to Google), and parsing is our own
        # sub-5-millisecond string work accounted for separately.
        duration_ms = _elapsed_ms(started_at)

        # Step 7 — parse AND re-validate. A schema in the request is a request, not a guarantee.
        try:
            documents = parse_tailoring_response(completion.text)
        except LlmOutputInvalid as invalid:
            self._log_invalid_output(invalid, attempt, duration_ms, completion)
            raise

        # Step 8 — the metrics. This is the ONLY thing about an LLM call this codebase may log, and
        # having it as one named type is what makes that rule easy to obey rather than merely easy
        # to state: no prompt and no completion is reachable from it.
        metrics = LlmCallMetrics(
            model=ModelName(self._settings.gemini_model),
            prompt_version=PromptVersion(PROMPT_VERSION),
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            duration_ms=duration_ms,
        )
        log.info(
            _EVENT_SUCCEEDED,
            model=metrics.model.value,
            prompt_version=metrics.prompt_version.value,
            prompt_tokens=metrics.prompt_tokens,
            completion_tokens=metrics.completion_tokens,
            duration_ms=metrics.duration_ms,
            attempt=attempt,
        )
        return TailoredDraft(documents=documents, metrics=metrics)

    # -- the real client -------------------------------------------------------------------------

    async def _generate_with_sdk(self, prompt: str) -> RawCompletion:
        """The strict default: one `generate_content` against the configured model.

        The only coroutine in the codebase that touches the SDK. It does no retrying, no timing and
        no error translation of its own — all three live in `_attempt_once`, so that the injected
        stub and the real client meet exactly the same machinery.
        """
        if self._client is None:
            # Built on first use. Two coroutines racing here would each build one and the last would
            # win, which costs one short-lived object and nothing else — a lock to prevent that would
            # be the more expensive mistake.
            self._client = genai.Client(api_key=self._settings.gemini_api_key)
        response = await self._client.aio.models.generate_content(
            model=self._settings.gemini_model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
                max_output_tokens=self._settings.llm_max_output_tokens,
            ),
        )
        return raw_completion_from_response(response)

    # -- logging ---------------------------------------------------------------------------------

    def _log_failure(
        self,
        failure_reason: str,
        attempt: int,
        error_type: str,
        started_at: float,
        **extra: object,
    ) -> None:
        """The one `llm.call_failed` shape (step 11): a fixed reason label, the attempt number, the
        exception's fully-qualified TYPE, and how long the attempt took.

        `error_type` is a class name and can carry no document content. `str(exc)` and `exc_info` are
        absent by design — a provider error message routinely quotes the request body, which here is
        a stranger's CV.
        """
        log.warning(
            _EVENT_FAILED,
            failure_reason=failure_reason,
            attempt=attempt,
            error_type=error_type,
            duration_ms=_elapsed_ms(started_at),
            **extra,
        )

    def _log_floor(self, attempt: int, exc: Exception, started_at: float) -> None:
        """The floor's line. Separate only so the two privacy rules have one place to be stated:
        fully-qualified type, never the message, never `exc_info`."""
        self._log_failure("llm_error", attempt, _qualified_type(exc), started_at)

    def _log_invalid_output(
        self, invalid: LlmOutputInvalid, attempt: int, duration_ms: int, completion: RawCompletion
    ) -> None:
        """G-20/G-21's line: the fixed `problem` label, and for `document_too_long` a character count.

        **The count is measured here from the raw completion, because the parser deliberately returns
        nothing derived from `raw`** — `LlmOutputInvalid` carries the label and only the label, so
        that no fragment of a half-written CV can travel inside an exception message into a log line
        or a Sentry frame. `len(completion.text)` is therefore the whole JSON response's length: an
        upper bound on the offending document rather than its exact size. That is stated rather than
        papered over, because a field named `character_count` invites being read as exact, and the
        honest approximation is worth more than a precise number bought by handing the parser's
        caller a slice of the document.
        """
        extra: dict[str, object] = {"problem": invalid.problem}
        if invalid.problem == PROBLEM_DOCUMENT_TOO_LONG:
            extra["character_count"] = len(completion.text)
        log.warning(
            _EVENT_FAILED,
            failure_reason=invalid.reason.value,
            attempt=attempt,
            error_type=_qualified_type(invalid),
            duration_ms=duration_ms,
            **extra,
        )


def _classify_api_error(exc: genai_errors.APIError) -> TailoringFailed | None:
    """Step 9's exception-shaped half: one SDK `APIError` → the domain's name for it, or `None`.

    `None` means *we have no better name than the floor's*, and returning it rather than guessing is
    what keeps `llm_error` honest: a 400 `INVALID_ARGUMENT` is a bug in our request, not a busy
    provider, and calling it `llm_unavailable` would invite a retry that cannot succeed and a "try
    again" button that cannot help.

    The `status` strings are gRPC canonical codes as the REST API returns them. Both `UNAVAILABLE`
    and `SERVICE_UNAVAILABLE` are accepted because the technical plan names the second and the API
    emits the first, and the cost of accepting both is nothing.
    """
    status = (exc.status or "").upper()
    code = exc.code or 0

    if code == 429 or status == "RESOURCE_EXHAUSTED":
        # Our quota, not the user's mistake. Retryable with backoff — PRD §6's retry indicator.
        return LlmRateLimited()

    # `DEADLINE_EXCEEDED` is classified as UNAVAILABLE rather than TIMED_OUT on purpose, and a reader
    # will want to reach for the obvious mapping. `llm_timed_out` means OUR deadline fired — the
    # per-attempt `wait_for` or the total one — and it is the signal the 15-second budget is watched
    # through. A provider reporting its own deadline is the provider failing to serve us, which is
    # what `llm_unavailable` says. Keeping the two apart is what lets the budget's rate be read
    # without the provider's outages moving it.
    if code >= 500 or status in {
        "UNAVAILABLE",
        "SERVICE_UNAVAILABLE",
        "INTERNAL",
        "DEADLINE_EXCEEDED",
        "ABORTED",
    }:
        return LlmUnavailable()

    return None


def _qualified_type(exc: BaseException) -> str:
    """`module.QualName` — the only thing about an exception this module is allowed to log."""
    return f"{type(exc).__module__}.{type(exc).__qualname__}"


def _elapsed_ms(started_at: float) -> int:
    """Milliseconds since `started_at`, from `time.perf_counter()`.

    `perf_counter` and not the `Clock` port, and the reason is in `LlmCallMetrics`' docstring: the
    port is whole-second by contract, which makes a subtraction accurate to ±1 s — 7 % of the thing a
    15-second budget is trying to measure. This measures a DURATION, never a "now"; it cannot date
    anything and never reaches an aggregate's timestamp field.
    """
    return round((time.perf_counter() - started_at) * 1000)


if TYPE_CHECKING:
    # Makes mypy prove this satisfies the port structurally rather than by eye.
    def _assert_implements_llm_port(adapter: GeminiLlm) -> None:
        _: LlmPort = adapter
