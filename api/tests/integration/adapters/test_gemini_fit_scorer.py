"""Adapter tests for `GeminiFitScorer` (`FitScorerPort`), written **after** the code (T27): the real
adapter against its injected `generate` seam, with no network and no real client.

**The fakes are built from the real SDK response shape.** The seam hands the adapter a
`RawCompletion`, so every scripted answer here is a real `GenerateContentResponse` pushed through the
production `raw_completion_from_response`, exactly as `_generate_with_sdk` does. A refusal is therefore
a blocked `prompt_feedback`, not a hand-raised `LlmRefused`, and missing usage is a response with no
`usage_metadata`.

**What is pinned on purpose** (ADR-0041 decision 3): an attempt is unmetered from the moment it starts
and stops being so only when usage arrives with both counts non-zero. So a refusal, a timeout, a failure
of any class, and a half-reported usage block all settle at their worst case. Those assertions say so
by name; they are decisions, not accidents.

Every test uses a placeholder, non-empty key, because the no-key refusal runs even when a `generate`
is injected. Backoff is zero everywhere but the test that is about it.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from tailorcraft.domain.scoring.errors import FitScoringFailed, FitScoringOutputInvalid
from tailorcraft.domain.scoring.value_objects import (
    MAX_FIT_GAPS,
    CvForScoring,
    FitScoreFailureReason,
    PostingForScoring,
)
from tailorcraft.infrastructure.llm.fit_score_parsing import (
    KEY_GAPS,
    KEY_POSTING_QUOTE,
    KEY_REQUIREMENT,
    KEY_SCORE,
)
from tailorcraft.infrastructure.llm.fit_scorer import (
    GeminiFitScorer,
    build_fit_score_config,
)
from tailorcraft.infrastructure.llm.gemini import GenerateFn, raw_completion_from_response
from tailorcraft.infrastructure.settings import Settings

_KEY = "placeholder-test-key-never-a-real-secret"
_QUOTE = "five years of Kubernetes experience"
_POSTING = f"We are hiring a platform engineer. You bring {_QUOTE} and calm."

Step = genai_types.GenerateContentResponse | BaseException


def _settings(base: Settings, **overrides: object) -> Settings:
    update: dict[str, object] = {
        "gemini_api_key": _KEY,
        "fit_score_retry_backoff_seconds": 0.0,
        **overrides,
    }
    return base.model_copy(update=update)


def _cv(text: str = "Platform engineer. Ran clusters for a bank.") -> CvForScoring:
    return CvForScoring(text)


def _posting(text: str = _POSTING) -> PostingForScoring:
    return PostingForScoring(text)


def _valid_text(score: int = 60) -> str:
    gap = {KEY_REQUIREMENT: "Kubernetes experience", KEY_POSTING_QUOTE: _QUOTE}
    return json.dumps({KEY_SCORE: score, KEY_GAPS: [gap]})


def _response(
    text: str,
    *,
    prompt_tokens: int | None = 100,
    output_tokens: int | None = 10,
    thoughts_tokens: int | None = None,
    with_usage: bool = True,
) -> genai_types.GenerateContentResponse:
    usage = (
        genai_types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=output_tokens,
            thoughts_token_count=thoughts_tokens,
        )
        if with_usage
        else None
    )
    return genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(role="model", parts=[genai_types.Part(text=text)]),
                finish_reason=genai_types.FinishReason.STOP,
            )
        ],
        usage_metadata=usage,
    )


def _blocked() -> genai_types.GenerateContentResponse:
    return genai_types.GenerateContentResponse(
        prompt_feedback=genai_types.GenerateContentResponsePromptFeedback(
            block_reason=genai_types.BlockedReason.SAFETY
        )
    )


def _api_error(code: int, status: str) -> genai_errors.APIError:
    return genai_errors.APIError(
        code, {"error": {"code": code, "status": status, "message": "vendor message"}}
    )


def _scripted(*steps: Step) -> tuple[GenerateFn, list[str]]:
    """Call n answers with step n (the last repeats). A response goes through the production
    translation; an exception is raised. Returns the stub and every prompt it saw."""
    prompts: list[str] = []

    async def stub(prompt: str):  # type: ignore[no-untyped-def]  # return type is RawCompletion
        prompts.append(prompt)
        step = steps[min(len(prompts), len(steps)) - 1]
        if isinstance(step, BaseException):
            raise step
        return raw_completion_from_response(step)

    return stub, prompts


# --- The happy path and its accounting --------------------------------------------------------------


async def test_a_good_answer_is_an_assessment_with_the_usage_the_provider_reported(
    settings: Settings,
) -> None:
    stub, prompts = _scripted(
        _response(_valid_text(60), prompt_tokens=1234, output_tokens=56, thoughts_tokens=7)
    )
    scorer = GeminiFitScorer(_settings(settings, fit_score_model="gemini-test"), generate=stub)

    fit = await scorer.assess(_cv(), _posting())

    assert len(prompts) == 1
    assert fit.assessment.score.value == 60
    assert len(fit.assessment.gaps) == 1
    usage = fit.usage
    assert usage.input_tokens == 1234
    # Thoughts bill at the output rate, so they count as output.
    assert usage.output_tokens == 56 + 7
    assert usage.unmetered_attempts == 0
    assert usage.model.value == "gemini-test"


# --- Each failure class: its reason, its attempts, and what it cost ----------------------------------


@pytest.mark.parametrize(
    ("step", "reason", "calls"),
    [
        (_api_error(429, "RESOURCE_EXHAUSTED"), FitScoreFailureReason.LLM_RATE_LIMITED, 2),
        (_api_error(500, "INTERNAL"), FitScoreFailureReason.LLM_UNAVAILABLE, 2),
        (_api_error(503, "UNAVAILABLE"), FitScoreFailureReason.LLM_UNAVAILABLE, 2),
        (httpx.ConnectError("refused"), FitScoreFailureReason.LLM_UNAVAILABLE, 2),
        (ConnectionError("reset"), FitScoreFailureReason.LLM_UNAVAILABLE, 2),
        (httpx.ReadTimeout("slow"), FitScoreFailureReason.LLM_TIMED_OUT, 2),
        (TimeoutError(), FitScoreFailureReason.LLM_TIMED_OUT, 2),
        # Our request was wrong, not the provider busy: retrying cannot help.
        (_api_error(400, "INVALID_ARGUMENT"), FitScoreFailureReason.LLM_ERROR, 1),
        # The floor: nothing the adapter named.
        (KeyError("anything"), FitScoreFailureReason.LLM_ERROR, 1),
        (AttributeError("anything"), FitScoreFailureReason.LLM_ERROR, 1),
    ],
    ids=[
        "429",
        "500",
        "503",
        "connect_error",
        "connection_error",
        "httpx_timeout",
        "builtin_timeout",
        "400_is_llm_error_no_retry",
        "floor_key_error",
        "floor_attribute_error",
    ],
)
async def test_each_failure_class_has_its_reason_and_is_retried_only_when_retrying_can_help(
    settings: Settings, step: BaseException, reason: FitScoreFailureReason, calls: int
) -> None:
    stub, prompts = _scripted(step)
    scorer = GeminiFitScorer(_settings(settings), generate=stub)

    with pytest.raises(FitScoringFailed) as raised:
        await scorer.assess(_cv(), _posting())

    assert raised.value.reason is reason
    assert len(prompts) == calls
    # No usage ever arrived, so every attempt settles at its worst case.
    assert raised.value.usage.unmetered_attempts == calls
    assert raised.value.usage.input_tokens == 0
    assert raised.value.usage.output_tokens == 0


async def test_a_refusal_is_attempted_once_and_settles_at_worst_case(settings: Settings) -> None:
    """S-3, on purpose: a block arrives as a 200 with no usage block we can believe, so the attempt
    stays unmetered. A refusal retried is a refusal repeated at twice the price."""
    stub, prompts = _scripted(_blocked())
    scorer = GeminiFitScorer(_settings(settings), generate=stub)

    with pytest.raises(FitScoringFailed) as raised:
        await scorer.assess(_cv(), _posting())

    assert raised.value.reason is FitScoreFailureReason.LLM_REFUSED
    assert len(prompts) == 1
    assert raised.value.usage.unmetered_attempts == 1


async def test_a_refusal_after_a_failed_first_attempt_is_not_retried_either(
    settings: Settings,
) -> None:
    stub, prompts = _scripted(_api_error(503, "UNAVAILABLE"), _blocked())
    scorer = GeminiFitScorer(_settings(settings), generate=stub)

    with pytest.raises(FitScoringFailed) as raised:
        await scorer.assess(_cv(), _posting())

    assert raised.value.reason is FitScoreFailureReason.LLM_REFUSED
    assert len(prompts) == 2
    assert raised.value.usage.unmetered_attempts == 2


@pytest.mark.parametrize(
    "text",
    ["not json at all", json.dumps({KEY_SCORE: 150, KEY_GAPS: []})],
    ids=["not_json", "wrong"],
)
async def test_output_that_is_unusable_is_retried_once_then_fails_as_output_invalid(
    settings: Settings, text: str
) -> None:
    """S-4 and S-5: a parse failure is billed, so both attempts are metered, and the retry is bounded."""
    stub, prompts = _scripted(_response(text, prompt_tokens=100, output_tokens=10))
    scorer = GeminiFitScorer(_settings(settings), generate=stub)

    with pytest.raises(FitScoringOutputInvalid) as raised:
        await scorer.assess(_cv(), _posting())

    assert raised.value.reason is FitScoreFailureReason.LLM_OUTPUT_INVALID
    assert len(prompts) == 2
    assert raised.value.usage.input_tokens == 200
    assert raised.value.usage.output_tokens == 20
    assert raised.value.usage.unmetered_attempts == 0


async def test_output_invalid_then_a_good_answer_succeeds_on_the_second_attempt(
    settings: Settings,
) -> None:
    stub, prompts = _scripted(_response("nope"), _response(_valid_text()))
    scorer = GeminiFitScorer(_settings(settings), generate=stub)

    fit = await scorer.assess(_cv(), _posting())

    assert len(prompts) == 2
    assert fit.assessment.score.value == 60


async def test_a_single_attempt_setting_makes_invalid_output_final(settings: Settings) -> None:
    stub, prompts = _scripted(_response("nope"))
    scorer = GeminiFitScorer(_settings(settings, fit_score_max_attempts=1), generate=stub)

    with pytest.raises(FitScoringOutputInvalid):
        await scorer.assess(_cv(), _posting())

    assert len(prompts) == 1


# --- Pre-flights: zero calls, zero cost -------------------------------------------------------------


async def test_a_cv_over_the_limit_is_refused_with_zero_calls_and_zero_usage(
    settings: Settings,
) -> None:
    stub, prompts = _scripted(_response(_valid_text()))
    scorer = GeminiFitScorer(_settings(settings, llm_max_cv_characters=20), generate=stub)

    with pytest.raises(FitScoringFailed) as raised:
        await scorer.assess(_cv("x" * 21), _posting())

    assert raised.value.reason is FitScoreFailureReason.INPUTS_TOO_LARGE
    assert prompts == []
    assert raised.value.usage.unmetered_attempts == 0
    assert raised.value.usage.input_tokens == 0


async def test_an_empty_key_refuses_with_zero_calls_even_with_a_stub_injected(
    settings: Settings,
) -> None:
    stub, prompts = _scripted(_response(_valid_text()))
    scorer = GeminiFitScorer(_settings(settings, gemini_api_key="  "), generate=stub)

    with pytest.raises(FitScoringFailed) as raised:
        await scorer.assess(_cv(), _posting())

    assert raised.value.reason is FitScoreFailureReason.LLM_UNAVAILABLE
    assert prompts == []
    assert raised.value.usage.unmetered_attempts == 0


# --- S-30 and usage across attempts ------------------------------------------------------------------


async def test_a_200_with_no_usage_block_keeps_the_assessment_and_counts_an_unmetered_attempt(
    settings: Settings,
) -> None:
    """S-30: the answer was paid for and is kept; the cost is unknown, so it is worst case."""
    stub, _ = _scripted(_response(_valid_text(), with_usage=False))
    scorer = GeminiFitScorer(_settings(settings), generate=stub)

    fit = await scorer.assess(_cv(), _posting())

    assert fit.assessment.score.value == 60
    assert fit.usage.unmetered_attempts == 1
    assert fit.usage.input_tokens == 0
    assert fit.usage.output_tokens == 0


@pytest.mark.parametrize(
    ("prompt_tokens", "output_tokens"),
    [(100, None), (None, 10), (100, 0), (0, 10)],
    ids=["no_output_count", "no_input_count", "zero_output", "zero_input"],
)
async def test_half_reported_usage_is_unmetered_and_its_tokens_are_not_counted(
    settings: Settings, prompt_tokens: int | None, output_tokens: int | None
) -> None:
    """On purpose (ADR-0041 decision 3): a real call reads at least one token each way, so a zero on
    either side is usage we did not get. Counting the half we have would understate; the attempt
    settles at worst case instead and the reported half is dropped."""
    stub, _ = _scripted(
        _response(_valid_text(), prompt_tokens=prompt_tokens, output_tokens=output_tokens)
    )
    scorer = GeminiFitScorer(_settings(settings), generate=stub)

    fit = await scorer.assess(_cv(), _posting())

    assert fit.usage.unmetered_attempts == 1
    assert fit.usage.input_tokens == 0
    assert fit.usage.output_tokens == 0


async def test_usage_accumulates_across_two_attempts(settings: Settings) -> None:
    stub, prompts = _scripted(
        _response("nope", prompt_tokens=100, output_tokens=10, thoughts_tokens=1),
        _response(_valid_text(), prompt_tokens=200, output_tokens=20, thoughts_tokens=2),
    )
    scorer = GeminiFitScorer(_settings(settings), generate=stub)

    fit = await scorer.assess(_cv(), _posting())

    assert len(prompts) == 2
    assert fit.usage.input_tokens == 300
    assert fit.usage.output_tokens == 33
    assert fit.usage.unmetered_attempts == 0


async def test_a_failed_first_attempt_stays_unmetered_beside_a_metered_second(
    settings: Settings,
) -> None:
    """The case `LlmCallMetrics` would undercount: the first call died after being billed, and only
    the surviving attempt reports usage."""
    stub, _ = _scripted(
        _api_error(503, "UNAVAILABLE"),
        _response(_valid_text(), prompt_tokens=200, output_tokens=20),
    )
    scorer = GeminiFitScorer(_settings(settings), generate=stub)

    fit = await scorer.assess(_cv(), _posting())

    assert fit.usage.unmetered_attempts == 1
    assert fit.usage.input_tokens == 200
    assert fit.usage.output_tokens == 20


# --- Timeouts count as unmetered ---------------------------------------------------------------------


async def test_a_per_attempt_timeout_is_llm_timed_out_and_counted_unmetered(
    settings: Settings,
) -> None:
    async def hangs(prompt: str):  # type: ignore[no-untyped-def]
        await asyncio.sleep(30)

    scorer = GeminiFitScorer(
        _settings(settings, fit_score_request_timeout_seconds=0.05, fit_score_max_attempts=1),
        generate=hangs,
    )

    with pytest.raises(FitScoringFailed) as raised:
        await scorer.assess(_cv(), _posting())

    assert raised.value.reason is FitScoreFailureReason.LLM_TIMED_OUT
    assert raised.value.usage.unmetered_attempts == 1
    # The provider's share of the time is recorded even though the attempt was cut off.
    assert raised.value.usage.duration_ms >= 40


async def test_the_total_deadline_cuts_off_an_attempt_in_flight_and_counts_it_unmetered(
    settings: Settings,
) -> None:
    async def hangs(prompt: str):  # type: ignore[no-untyped-def]
        await asyncio.sleep(30)

    scorer = GeminiFitScorer(
        _settings(
            settings,
            fit_score_request_timeout_seconds=30.0,
            fit_score_total_deadline_seconds=1,
        ),
        generate=hangs,
    )

    with pytest.raises(FitScoringFailed) as raised:
        await scorer.assess(_cv(), _posting())

    assert raised.value.reason is FitScoreFailureReason.LLM_TIMED_OUT
    assert raised.value.usage.unmetered_attempts == 1
    assert raised.value.usage.duration_ms >= 900


# --- Cancellation passes the floor --------------------------------------------------------------------


async def test_a_cancelled_error_raised_by_the_call_is_not_translated(settings: Settings) -> None:
    """AC-21: `except Exception`, never `BaseException`. Injected below the floor, at the seam."""
    stub, _ = _scripted(asyncio.CancelledError())
    scorer = GeminiFitScorer(_settings(settings), generate=stub)

    with pytest.raises(asyncio.CancelledError):
        await scorer.assess(_cv(), _posting())


async def test_cancelling_the_task_mid_call_cancels_rather_than_failing(settings: Settings) -> None:
    started = asyncio.Event()

    async def slow(prompt: str):  # type: ignore[no-untyped-def]
        started.set()
        await asyncio.sleep(30)

    scorer = GeminiFitScorer(
        _settings(settings, fit_score_request_timeout_seconds=30.0), generate=slow
    )
    task = asyncio.ensure_future(scorer.assess(_cv(), _posting()))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --- The prompt and the config ------------------------------------------------------------------------


async def test_the_prompt_carries_the_two_inputs_and_the_parsers_keys_and_nothing_else(
    settings: Settings,
) -> None:
    cv_sentinel = "CV-SENTINEL-Jane-Q-Doe-employment-history-8271"
    posting_sentinel = "POSTING-SENTINEL-Northwind-Logistics-role-4462"
    stub, prompts = _scripted(_response(_valid_text()))
    scorer = GeminiFitScorer(_settings(settings), generate=stub)

    await scorer.assess(
        _cv(f"Platform engineer. {cv_sentinel}"), _posting(f"{_POSTING} {posting_sentinel}")
    )

    assert len(prompts) == 1
    prompt = prompts[0]
    assert cv_sentinel in prompt
    assert posting_sentinel in prompt
    # The model must be told the keys the parser then demands.
    for key in (KEY_SCORE, KEY_GAPS, KEY_REQUIREMENT, KEY_POSTING_QUOTE):
        assert f'"{key}"' in prompt
    # Structurally unreachable by a two-string signature; asserted anyway as the proof.
    for absent in (
        "careless-leak@example.com",
        "11111111-aaaa-4bbb-8ccc-222222222222",
        "203.0.113.42",
        _KEY,
    ):
        assert absent not in prompt


def test_the_request_config_is_what_the_adapter_promises(settings: Settings) -> None:
    """The seam never sees the config (plan §0.7), so this is the only place it can be pinned."""
    config = build_fit_score_config(_settings(settings, fit_score_max_output_tokens=321))

    assert config.response_mime_type == "application/json"
    assert config.max_output_tokens == 321
    assert config.temperature == 0.0
    assert isinstance(config.seed, int)
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_budget == 0
    schema = config.response_schema
    assert isinstance(schema, genai_types.Schema)
    assert schema.required == [KEY_SCORE, KEY_GAPS]
    assert schema.properties is not None
    assert schema.properties[KEY_SCORE].type == genai_types.Type.INTEGER
    gaps = schema.properties[KEY_GAPS]
    assert gaps.type == genai_types.Type.ARRAY
    assert gaps.max_items == MAX_FIT_GAPS
    assert gaps.items is not None
    assert gaps.items.required == [KEY_REQUIREMENT, KEY_POSTING_QUOTE]


# --- Privacy: nothing from the CV, posting or completion leaves through a log or an exception ----------

_CV_SECRET = "CV-SECRET-12-Elm-Street-Springfield-7731"
_POSTING_SECRET = "POSTING-SECRET-Northwind-Logistics-9921"
_ERROR_SECRET = "ERROR-SECRET-quoted-by-a-vendor-message-5518"
_COMPLETION_SECRET = "COMPLETION-SECRET-half-a-cv-in-the-model-answer-4402"
_SECRETS = (_CV_SECRET, _POSTING_SECRET, _ERROR_SECRET, _COMPLETION_SECRET)


def _assert_nothing_leaked(failure: FitScoringFailed, logs: str) -> None:
    for secret in _SECRETS:
        assert secret not in logs, f"{secret} reached a log line"
        assert secret not in str(failure)
        assert secret not in repr(failure)
        assert secret not in repr(failure.args)
    assert failure.__cause__ is None
    assert failure.__suppress_context__ is True


@pytest.mark.parametrize(
    "step",
    [
        KeyError(_ERROR_SECRET),
        _api_error(503, "UNAVAILABLE"),
        httpx.ConnectError(_ERROR_SECRET),
        _response(f"not json {_COMPLETION_SECRET}"),
        _blocked(),
    ],
    ids=["floor", "api_error", "transport", "invalid_output", "refusal"],
)
async def test_a_failure_leaks_no_input_message_or_completion_into_logs_or_the_exception(
    settings: Settings, caplog: pytest.LogCaptureFixture, step: Step
) -> None:
    stub, _ = _scripted(step)
    scorer = GeminiFitScorer(_settings(settings), generate=stub)
    cv = _cv(f"Engineer. {_CV_SECRET}")
    posting = _posting(f"{_POSTING} {_POSTING_SECRET}")

    with caplog.at_level(logging.DEBUG), pytest.raises(FitScoringFailed) as raised:
        await scorer.assess(cv, posting)

    # Not vacuous: the adapter did log, and the type of the failure is what it logs.
    assert "scoring.call_failed" in caplog.text
    _assert_nothing_leaked(raised.value, caplog.text)


async def test_the_floor_logs_the_exception_type_but_not_its_message(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    stub, _ = _scripted(KeyError(_ERROR_SECRET))
    scorer = GeminiFitScorer(_settings(settings), generate=stub)

    with caplog.at_level(logging.DEBUG), pytest.raises(FitScoringFailed):
        await scorer.assess(_cv(), _posting())

    assert "KeyError" in caplog.text
    assert _ERROR_SECRET not in caplog.text


async def test_a_success_logs_counts_but_not_the_inputs_or_the_gaps(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    stub, _ = _scripted(_response(_valid_text()))
    scorer = GeminiFitScorer(_settings(settings), generate=stub)

    with caplog.at_level(logging.DEBUG):
        await scorer.assess(
            _cv(f"Engineer. {_CV_SECRET}"), _posting(f"{_POSTING} {_POSTING_SECRET}")
        )

    assert "scoring.call_succeeded" in caplog.text
    for secret in (_CV_SECRET, _POSTING_SECRET, _QUOTE, "Kubernetes experience"):
        assert secret not in caplog.text


# --- aclose -------------------------------------------------------------------------------------------


async def test_aclose_with_an_injected_generate_is_a_safe_no_op_twice(settings: Settings) -> None:
    scorer = GeminiFitScorer(_settings(settings), generate=_scripted(_response("x"))[0])

    await scorer.aclose()
    await scorer.aclose()
