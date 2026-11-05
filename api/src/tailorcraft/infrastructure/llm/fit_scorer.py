"""`GeminiFitScorer`: the `FitScorerPort` adapter (slice 5.1, ADR-0040, ADR-0041).

The same shape as `GeminiLlm`, for the same reasons, and read beside it: a `generate` seam with the real
client as the strict default, a pre-flight, one hard deadline over bounded attempts, and an `except
Exception` floor making the port's promise true by construction. No retry loop is shared: making one
generic would touch the paid tailoring path to save forty lines (AC-43).

**What is new is the accounting.** `LlmCallMetrics` describes the surviving attempt, and a cap built on
that would undercount exactly the calls that go wrong. So every attempt is counted as unmetered from the
moment it starts, and stops being so only when usage arrives. A failure of any class, and a cancelled
attempt under the deadline, therefore cost their worst case without anyone having to remember to say so
(ADR-0041 decision 3). The total rides on the success and on the exception.

**The client is built in the constructor, and the constructor runs in the lifespan** (AC-22): building
an SSL context blocks, so it must not happen on a request. A 5.2 worker binds this class per task, with
`aclose` in a `finally`, as `tailoring_use_case` does.

**Nothing from the prompt or the completion is logged**, nor a gap, a quote or a fingerprint: only
counts, durations, fixed labels and exception type names. The CV sits in a local here for up to 14 s,
which is why every raise is `from None` (Constitution §8).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Final

import httpx
import structlog
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from tailorcraft.domain.scoring.errors import FitScoringFailed, FitScoringOutputInvalid
from tailorcraft.domain.scoring.value_objects import (
    MAX_FIT_GAPS,
    AssessedFit,
    CvForScoring,
    FitAssessment,
    FitScoreFailureReason,
    ModelName,
    PostingForScoring,
    PromptVersion,
    ScoringUsage,
)
from tailorcraft.domain.tailoring.errors import LlmUnavailable, TailoringFailed
from tailorcraft.infrastructure.llm.fit_score_parsing import (
    KEY_GAPS,
    KEY_POSTING_QUOTE,
    KEY_REQUIREMENT,
    KEY_SCORE,
    FitScoreResponseInvalid,
    parse_fit_score_response,
)
from tailorcraft.infrastructure.llm.fit_score_prompt import (
    FIT_SCORE_PROMPT_VERSION,
    build_fit_score_prompt,
)
from tailorcraft.infrastructure.llm.gemini import (
    GenerateFn,
    RawCompletion,
    classify_api_error,
    elapsed_ms,
    qualified_type,
    raw_completion_from_response,
)
from tailorcraft.infrastructure.settings import Settings

if TYPE_CHECKING:
    from tailorcraft.domain.scoring.ports import FitScorerPort

log = structlog.get_logger(__name__)

# The prompt's own characters, measured from the real prompt rather than written down: the reservation's
# upper bound (ADR-0041 decision 2) is `cv + posting + this`, and a stale constant would break it
# silently the day the prompt grows. The composition root hands it to `RequestFitScore`.
PROMPT_OVERHEAD_CHARACTERS: Final = len(build_fit_score_prompt("", ""))

# Not `LLM_REFUSED`: a refusal retried is a refusal repeated at twice the price (S-3).
_RETRYABLE: Final = frozenset(
    {
        FitScoreFailureReason.LLM_UNAVAILABLE,
        FitScoreFailureReason.LLM_RATE_LIMITED,
        FitScoreFailureReason.LLM_TIMED_OUT,
        FitScoreFailureReason.LLM_OUTPUT_INVALID,
    }
)

# Any fixed number. The SDK promises only a "best effort" for it, and AC-51 measures what it delivers.
_SEED: Final = 5151

# A hint. `parse_fit_score_response` is the guarantee, and runs on every response regardless.
_RESPONSE_SCHEMA: Final = genai_types.Schema(
    type=genai_types.Type.OBJECT,
    properties={
        KEY_SCORE: genai_types.Schema(type=genai_types.Type.INTEGER, minimum=0, maximum=100),
        KEY_GAPS: genai_types.Schema(
            type=genai_types.Type.ARRAY,
            max_items=MAX_FIT_GAPS,
            items=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    KEY_REQUIREMENT: genai_types.Schema(type=genai_types.Type.STRING),
                    KEY_POSTING_QUOTE: genai_types.Schema(type=genai_types.Type.STRING),
                },
                required=[KEY_REQUIREMENT, KEY_POSTING_QUOTE],
            ),
        ),
    },
    required=[KEY_SCORE, KEY_GAPS],
)


def build_fit_score_config(settings: Settings) -> genai_types.GenerateContentConfig:
    """The request config every real call sends. Public and pure because the `generate` seam never
    sees it, so a test can only assert on it here (plan §0.7).

    Every field is checked against the installed google-genai (2.23.0), and the mapping to the wire
    against `_GenerateContentConfig_to_mldev`, with no API call. **`thinking_budget=0` is the one thing
    that cannot be checked offline:** its doc says "model dependent". T51 reads `thoughts_token_count`
    on real calls, and a model that rejects it answers 400, which lands on the floor as `llm_error` on
    every call. Change this line in the same commit as `fit_score_model`.
    """
    return genai_types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=_RESPONSE_SCHEMA,
        max_output_tokens=settings.fit_score_max_output_tokens,
        temperature=0.0,
        seed=_SEED,
        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
    )


class _Tally:
    """What one `assess` has consumed so far, across its attempts."""

    __slots__ = ("attempts", "duration_ms", "input_tokens", "output_tokens", "unmetered_attempts")

    def __init__(self) -> None:
        self.attempts = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.unmetered_attempts = 0
        self.duration_ms = 0

    def begin_attempt(self) -> None:
        self.attempts += 1
        self.unmetered_attempts += 1

    def meter(self, completion: RawCompletion) -> None:
        """Move the attempt in flight from unmetered to metered, if its usage can be believed.

        `raw_completion_from_response` folds a missing usage block to zeros, and a real call reads at
        least one token each way, so a zero on either side is usage we did not get (S-30). Treating a
        half-reported one as unmetered overstates, which is the safe way to be wrong. Thoughts bill
        at the output rate, so they count as output.
        """
        if completion.prompt_tokens == 0 or completion.completion_tokens == 0:
            return
        self.unmetered_attempts -= 1
        self.input_tokens += completion.prompt_tokens
        self.output_tokens += completion.completion_tokens + (completion.thoughts_tokens or 0)


def _reason_of(failure: TailoringFailed) -> FitScoreFailureReason:
    """Tailoring's name for a failure, in this context's vocabulary. The shared helpers raise
    tailoring's errors. A reason with no twin here has nothing better to be called than the floor's."""
    try:
        return FitScoreFailureReason(failure.reason.value)
    except ValueError:
        return FitScoreFailureReason.LLM_ERROR


class GeminiFitScorer:
    """Judge one CV against one posting, or fail with a reason and what the attempts consumed.

    `generate` is a constructor argument with the real SDK client as the strict default, and only a test
    injects one. **The no-key refusal runs even when one is injected**, as in `GeminiLlm`, so the test
    and production paths share that check.
    """

    # ponytail: no semaphore on calls. The IP limit and the ledger bound the money; add a per-process
    # one if a burst from many IPs is ever seen rate-limiting tailoring on the shared key (plan §0.2).

    def __init__(self, settings: Settings, generate: GenerateFn | None = None) -> None:
        self._settings = settings
        self._model = ModelName(settings.fit_score_model)
        self._prompt_version = PromptVersion(FIT_SCORE_PROMPT_VERSION)
        self._client: genai.Client | None = None
        if generate is None and settings.gemini_api_key.strip():
            self._client = genai.Client(api_key=settings.gemini_api_key)
        self._generate: GenerateFn = generate if generate is not None else self._generate_with_sdk

    @property
    def generate(self) -> GenerateFn:
        """The generate function `assess` calls: the real SDK path unless one was injected.

        Read-only and public for one caller, `GeminiLlm.generate`'s reason: `eval-fit-scores` wraps the
        REAL path in `RecordingGenerate` and injects the recorder into a second scorer, so every attempt's
        usage is visible while all of `assess` still runs. It grants nothing `assess` did not.
        """
        return self._generate

    async def assess(self, cv: CvForScoring, posting: PostingForScoring) -> AssessedFit:
        """Raises `FitScoringFailed` on every failure, guaranteed by the floor below."""
        tally = _Tally()
        started_at = time.perf_counter()
        self._refuse_before_any_call(cv, tally)
        log.info(
            "scoring.call_started",
            model=self._model.value,
            prompt_version=self._prompt_version.value,
            cv_character_count=cv.character_count,
            posting_character_count=posting.character_count,
        )
        prompt = build_fit_score_prompt(cv.value, posting.value)
        try:
            # The hard bound over everything, backoff included. A second attempt gets what is left.
            assessment = await asyncio.wait_for(
                self._attempt_all(prompt, posting.value, tally),
                timeout=self._settings.fit_score_total_deadline_seconds,
            )
        except TimeoutError:
            raise self._failed(
                FitScoreFailureReason.LLM_TIMED_OUT,
                tally,
                "builtins.TimeoutError",
                started_at,
                deadline="total",
            ) from None
        except FitScoringFailed:
            raise
        except Exception as exc:
            # THE FLOOR. The surface underneath is the SDK's tree, httpx's, pydantic's and whatever a
            # transitive dependency raises, and an allow-list is a bet on having listed them all.
            # `Exception`, never `BaseException`: a cancellation must cancel (AC-21). The type only,
            # since a provider's message can quote the request body.
            raise self._failed(
                FitScoreFailureReason.LLM_ERROR, tally, qualified_type(exc), started_at
            ) from None

        usage = self._usage(tally)
        log.info(
            "scoring.call_succeeded",
            model=usage.model.value,
            prompt_version=usage.prompt_version.value,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            unmetered_attempts=usage.unmetered_attempts,
            duration_ms=usage.duration_ms,
            attempts=tally.attempts,
            gap_count=len(assessment.gaps),
        )
        return AssessedFit(assessment=assessment, usage=usage)

    def _refuse_before_any_call(self, cv: CvForScoring, tally: _Tally) -> None:
        """Both refusals make zero calls, so both settle to zero. Reject, never truncate (S-9)."""
        limit = self._settings.llm_max_cv_characters
        if cv.character_count > limit:
            log.info(
                "scoring.inputs_too_large",
                base_cv_character_count=cv.character_count,
                limit=limit,
                llm_call_skipped=True,
            )
            raise FitScoringFailed(FitScoreFailureReason.INPUTS_TOO_LARGE, self._usage(tally))
        if not self._settings.gemini_api_key.strip():
            log.warning("llm.not_configured", model=self._model.value, llm_call_skipped=True)
            raise FitScoringFailed(FitScoreFailureReason.LLM_UNAVAILABLE, self._usage(tally))

    async def _attempt_all(self, prompt: str, posting: str, tally: _Tally) -> FitAssessment:
        """At most `fit_score_max_attempts`, which `Settings` caps at 2, so invalid output is retried
        at most once (AC-24). Only `_RETRYABLE`, and never without backoff."""
        while True:
            tally.begin_attempt()
            try:
                return await self._attempt_once(prompt, posting, tally)
            except FitScoringFailed as failure:
                if (
                    tally.attempts >= self._settings.fit_score_max_attempts
                    or failure.reason not in _RETRYABLE
                ):
                    raise
            await asyncio.sleep(self._settings.fit_score_retry_backoff_seconds)

    async def _attempt_once(self, prompt: str, posting: str, tally: _Tally) -> FitAssessment:
        """One call, translated. The specific classes carry the better reason; anything else goes
        to the floor in `assess`."""
        started_at = time.perf_counter()
        try:
            completion = await self._timed_generate(prompt, tally)
        except TimeoutError:
            raise self._failed(
                FitScoreFailureReason.LLM_TIMED_OUT,
                tally,
                "builtins.TimeoutError",
                started_at,
                deadline="per_attempt",
            ) from None
        except TailoringFailed as failure:
            # `LlmRefused` from `raw_completion_from_response`, in tailoring's vocabulary.
            raise self._failed(
                _reason_of(failure), tally, qualified_type(failure), started_at
            ) from None
        except genai_errors.APIError as exc:
            classified = classify_api_error(exc)
            reason = (
                FitScoreFailureReason.LLM_ERROR if classified is None else _reason_of(classified)
            )
            raise self._failed(reason, tally, qualified_type(exc), started_at) from None
        except httpx.TimeoutException:
            # A per-phase transport timeout, which is NOT a builtin `TimeoutError`.
            raise self._failed(
                FitScoreFailureReason.LLM_TIMED_OUT,
                tally,
                "httpx.TimeoutException",
                started_at,
                deadline="transport",
            ) from None
        except (httpx.TransportError, ConnectionError) as exc:
            raise self._failed(
                FitScoreFailureReason.LLM_UNAVAILABLE, tally, qualified_type(exc), started_at
            ) from None

        # Before the parse: an answer that is thrown away was still billed.
        tally.meter(completion)
        try:
            # JSON, regexes and case-folding 30,000 characters do not belong on the loop (AC-22).
            return await asyncio.to_thread(parse_fit_score_response, completion.text, posting)
        except FitScoreResponseInvalid as invalid:
            log.warning(
                "scoring.call_failed",
                failure_reason=FitScoreFailureReason.LLM_OUTPUT_INVALID.value,
                problem=invalid.problem,
                attempt=tally.attempts,
                error_type=qualified_type(invalid),
                duration_ms=elapsed_ms(started_at),
            )
            raise FitScoringOutputInvalid(invalid.problem, self._usage(tally)) from None

    async def _timed_generate(self, prompt: str, tally: _Tally) -> RawCompletion:
        """The per-attempt bound, and the provider's share of the time. `finally`, so an attempt that
        fails or is cancelled by the outer deadline is still counted."""
        started_at = time.perf_counter()
        try:
            return await asyncio.wait_for(
                self._generate(prompt), timeout=self._settings.fit_score_request_timeout_seconds
            )
        finally:
            tally.duration_ms += elapsed_ms(started_at)

    async def _generate_with_sdk(self, prompt: str) -> RawCompletion:
        """The strict default, and the only coroutine here that touches the SDK. `None` only after
        `aclose`, when no new call should start."""
        client = self._client
        if client is None:
            raise LlmUnavailable()
        response = await client.aio.models.generate_content(
            model=self._model.value,
            contents=prompt,
            config=build_fit_score_config(self._settings),
        )
        return raw_completion_from_response(response)

    async def aclose(self) -> None:
        """Close both halves of the client, as `GeminiLlm.aclose` does and for its reasons: each closes
        only its own, and neither raises from here, since this runs at shutdown or in a `finally`."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.aio.aclose()
            client.close()
        except Exception as exc:
            log.warning("scoring.client_close_failed", error_type=qualified_type(exc))

    def _usage(self, tally: _Tally) -> ScoringUsage:
        return ScoringUsage(
            input_tokens=tally.input_tokens,
            output_tokens=tally.output_tokens,
            unmetered_attempts=tally.unmetered_attempts,
            duration_ms=tally.duration_ms,
            model=self._model,
            prompt_version=self._prompt_version,
        )

    def _failed(
        self,
        reason: FitScoreFailureReason,
        tally: _Tally,
        error_type: str,
        started_at: float,
        **extra: object,
    ) -> FitScoringFailed:
        """The one `scoring.call_failed` shape: a fixed reason, the attempt, the exception's TYPE and
        the time. `str(exc)` and `exc_info` are absent by design."""
        log.warning(
            "scoring.call_failed",
            failure_reason=reason.value,
            attempt=tally.attempts,
            error_type=error_type,
            duration_ms=elapsed_ms(started_at),
            **extra,
        )
        return FitScoringFailed(reason, self._usage(tally))


if TYPE_CHECKING:
    # Makes mypy prove this satisfies the port structurally rather than by eye.
    def _assert_implements_fit_scorer_port(adapter: GeminiFitScorer) -> None:
        _: FitScorerPort = adapter
