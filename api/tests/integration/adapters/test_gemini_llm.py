"""Adapter tests for `GeminiLlm` (`LlmPort`), written **after** (T34): the real adapter against its
injected `GenerateFn` seam — no network, no real SDK client — plus one section that deliberately
drives the **real** `google.genai` client against a local stub HTTP server, because a stub can prove
nothing about what the vendor library itself logs (1.2's `/verify` finding: `httpx` logged full URLs
one frame below a clean adapter, invisible to a test that drove a fake).

**The stub lives only here.** Nothing under `api/src/` ever constructs one — `GeminiLlm.__init__`'s
`generate` argument defaults to the real SDK call, and the wiring test at the bottom proves the
production binding never overrides it.

**Every happy-path and retry test below uses a placeholder, non-empty `gemini_api_key`.** The no-key
refusal (G-32) is unconditional and checked even when a stub is injected (`GeminiLlm`'s own
docstring explains why): a test that wants to exercise the retry loop or the retryable-failure
translations must first get past that gate, exactly as production would.

Mirrors `test_posting_fetcher.py`'s shape: happy path, failure-contract translation, the two floor
tests (an unrecognised exception, `asyncio.CancelledError` not swallowed), a privacy test against the
real adapter's own logging, and a wiring test.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import logging
import threading
from collections.abc import Callable

import httpx
import pytest
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
)
from tailorcraft.infrastructure.api.deps import get_llm
from tailorcraft.infrastructure.llm.gemini import (
    GeminiLlm,
    GenerateFn,
    RawCompletion,
    build_generate_config,
    raw_completion_from_response,
)
from tailorcraft.infrastructure.llm.parsing import KEY_COVER_LETTER, KEY_TAILORED_CV
from tailorcraft.infrastructure.llm.prompt import PROMPT_VERSION
from tailorcraft.infrastructure.settings import Settings

# --- Fixture content ---------------------------------------------------------------------------------

_PLACEHOLDER_KEY = "placeholder-test-key-never-a-real-secret"


def _settings(base: Settings, **overrides: object) -> Settings:
    """`base` (the session-scoped, test-database-pointed fixture) with a placeholder key and any
    LLM-specific override this test needs, via `model_copy` — which does not re-run
    `Settings`' own boot-guard validator, exactly as `conftest.py::settings` itself relies on."""
    return base.model_copy(update={"gemini_api_key": _PLACEHOLDER_KEY, **overrides})


def _cv(text: str = "word " * 200) -> ExtractedText:
    return ExtractedText(text)


def _posting(text: str = "x" * 150) -> JobPostingText:
    return JobPostingText(text)


def _valid_response_text(cv_chars: int = 450, letter_chars: int = 250) -> str:
    """A JSON body that clears both documents' floors (400 / 200 non-whitespace characters) and
    stays under both ceilings (20,000 / 8,000)."""
    payload = {KEY_TAILORED_CV: "a" * cv_chars, KEY_COVER_LETTER: "b" * letter_chars}
    return json.dumps(payload)


def _recording_stub() -> tuple[GenerateFn, list[str]]:
    """A `GenerateFn` that succeeds once, recording every prompt it was called with."""
    calls: list[str] = []

    async def stub(prompt: str) -> RawCompletion:
        calls.append(prompt)
        return RawCompletion(text=_valid_response_text(), prompt_tokens=111, completion_tokens=222)

    return stub, calls


def _always_raises(exc_factory: Callable[[], BaseException]) -> tuple[GenerateFn, list[int]]:
    """A `GenerateFn` that raises a fresh exception from `exc_factory` on every call, recording how
    many times it was actually invoked — the thing AC-8's retry-count assertions read."""
    calls: list[int] = []

    async def stub(prompt: str) -> RawCompletion:
        calls.append(1)
        raise exc_factory()

    return stub, calls


def _always_invalid() -> tuple[GenerateFn, list[int]]:
    """A `GenerateFn` that always answers with unparseable text — the natural way to drive
    `LlmOutputInvalid`, which is raised by `parse_tailoring_response`, not by the stub itself."""
    calls: list[int] = []

    async def stub(prompt: str) -> RawCompletion:
        calls.append(1)
        return RawCompletion(text="not json at all", prompt_tokens=1, completion_tokens=1)

    return stub, calls


def _api_error(
    code: int, status: str, message: str = "vendor message, never logged"
) -> genai_errors.APIError:
    """A real `google.genai.errors.APIError`, built the way the installed SDK actually derives
    `.code` and `.status` from a response body — measured directly against the installed
    `google-genai`, not assumed (see this module's own experiment in the QA report)."""
    return genai_errors.APIError(
        code, {"error": {"code": code, "status": status, "message": message}}
    )


# --- Happy path: correct metrics, and missing usage metadata records zeros (not a failure) ---------


async def test_the_happy_path_records_correct_llm_call_metrics(settings: Settings) -> None:
    stub, calls = _recording_stub()
    adapter = GeminiLlm(_settings(settings, gemini_model="gemini-test-model"), generate=stub)

    draft = await adapter.tailor(_cv(), _posting())

    assert draft.metrics.model.value == "gemini-test-model"
    assert draft.metrics.prompt_version.value == PROMPT_VERSION
    assert draft.metrics.prompt_tokens == 111
    assert draft.metrics.completion_tokens == 222
    assert draft.metrics.duration_ms >= 0
    assert draft.documents.cv.value == "a" * 450
    assert draft.documents.cover_letter.value == "b" * 250
    assert len(calls) == 1


def test_missing_usage_metadata_in_a_real_response_records_zero_token_counts() -> None:
    """`raw_completion_from_response`'s own rule: a provider that reports no usage metadata is a gap
    in accounting, not a reason to fail a run that produced two good documents (G-16…G-23 are silent
    on this because it is not a failure at all)."""
    response = genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(
                    role="model", parts=[genai_types.Part(text=_valid_response_text())]
                ),
                finish_reason=genai_types.FinishReason.STOP,
            )
        ]
        # usage_metadata deliberately omitted.
    )

    completion = raw_completion_from_response(response)

    assert completion.prompt_tokens == 0
    assert completion.completion_tokens == 0
    assert completion.text == _valid_response_text()


# --- Refusals are response-shaped, not exception-shaped ---------------------------------------------


def test_a_blocked_prompt_feedback_is_a_refusal() -> None:
    response = genai_types.GenerateContentResponse(
        prompt_feedback=genai_types.GenerateContentResponsePromptFeedback(
            block_reason=genai_types.BlockedReason.SAFETY
        )
    )

    with pytest.raises(LlmRefused):
        raw_completion_from_response(response)


@pytest.mark.parametrize(
    "finish_reason",
    [
        genai_types.FinishReason.SAFETY,
        genai_types.FinishReason.PROHIBITED_CONTENT,
        genai_types.FinishReason.BLOCKLIST,
        genai_types.FinishReason.SPII,
        genai_types.FinishReason.RECITATION,
    ],
)
def test_a_refusal_finish_reason_on_the_only_candidate_is_a_refusal(
    finish_reason: genai_types.FinishReason,
) -> None:
    response = genai_types.GenerateContentResponse(
        candidates=[genai_types.Candidate(finish_reason=finish_reason)]
    )

    with pytest.raises(LlmRefused):
        raw_completion_from_response(response)


def test_a_max_tokens_finish_reason_is_deliberately_not_a_refusal() -> None:
    """The one finish reason NOT in `_REFUSAL_FINISH_REASONS`: a truncated answer is retryable
    (`parse_tailoring_response` will label it `not_json`), and relabelling it a refusal would
    suppress the retry and hide that the real cause is `llm_max_output_tokens`."""
    response = genai_types.GenerateContentResponse(
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(parts=[genai_types.Part(text='{"truncat')]),
                finish_reason=genai_types.FinishReason.MAX_TOKENS,
            )
        ]
    )

    completion = raw_completion_from_response(response)

    assert completion.text == '{"truncat'


# --- Every vendor failure shape translates to its domain error --------------------------------------


@pytest.mark.parametrize(
    ("exc_factory", "expected"),
    [
        (lambda: _api_error(429, "RESOURCE_EXHAUSTED"), LlmRateLimited),
        (lambda: _api_error(500, "INTERNAL"), LlmUnavailable),
        (lambda: _api_error(503, "UNAVAILABLE"), LlmUnavailable),
        (lambda: _api_error(504, "DEADLINE_EXCEEDED"), LlmUnavailable),
        (lambda: _api_error(400, "INVALID_ARGUMENT"), LlmError),
        (lambda: httpx.TimeoutException("read timed out"), LlmTimedOut),
        (lambda: httpx.ConnectError("connection refused"), LlmUnavailable),
        (lambda: ConnectionError("connection reset"), LlmUnavailable),
    ],
    ids=[
        "429_resource_exhausted",
        "500_internal",
        "503_unavailable",
        "504_deadline_exceeded",
        "400_invalid_argument_is_unrecognised",
        "httpx_timeout_exception",
        "httpx_connect_error",
        "builtin_connection_error",
    ],
)
async def test_each_vendor_failure_shape_translates_to_its_domain_error(
    settings: Settings,
    exc_factory: Callable[[], BaseException],
    expected: type[Exception],
) -> None:
    stub, _calls = _always_raises(exc_factory)
    adapter = GeminiLlm(
        _settings(settings, llm_max_attempts=2, llm_retry_backoff_seconds=0.0), generate=stub
    )

    with pytest.raises(expected):
        await adapter.tailor(_cv(), _posting())


def test_httpx_timeout_exception_is_not_a_builtin_timeouterror_subclass() -> None:
    """1.2 shipped this exact gap in the fetcher: `httpx.TimeoutException` is NOT a subclass of the
    builtin `TimeoutError`, so a handler written for the builtin alone lets it fall through to a
    later, wrong branch. Measured against the installed httpx rather than assumed."""
    assert not issubclass(httpx.TimeoutException, TimeoutError)


# --- AC-8: retry counts — 2 for the four retryable classes, 1 for a refusal -------------------------


async def test_a_retryable_failure_is_attempted_twice(settings: Settings) -> None:
    stub, calls = _always_raises(lambda: httpx.ConnectError("refused"))
    adapter = GeminiLlm(
        _settings(settings, llm_max_attempts=2, llm_retry_backoff_seconds=0.0), generate=stub
    )

    with pytest.raises(LlmUnavailable):
        await adapter.tailor(_cv(), _posting())

    assert len(calls) == 2


async def test_llm_output_invalid_is_retryable_and_attempted_twice(settings: Settings) -> None:
    stub, calls = _always_invalid()
    adapter = GeminiLlm(
        _settings(settings, llm_max_attempts=2, llm_retry_backoff_seconds=0.0), generate=stub
    )

    with pytest.raises(LlmOutputInvalid):
        await adapter.tailor(_cv(), _posting())

    assert len(calls) == 2


async def test_a_refusal_is_attempted_exactly_once(settings: Settings) -> None:
    """AC-8/G-18: a refusal retried is a refusal repeated at twice the price. `_RETRYABLE` deliberately
    excludes `LlmRefused`, so even with `llm_max_attempts=2` configured, the stub is called once."""
    stub, calls = _always_raises(LlmRefused)
    adapter = GeminiLlm(
        _settings(settings, llm_max_attempts=2, llm_retry_backoff_seconds=0.0), generate=stub
    )

    with pytest.raises(LlmRefused):
        await adapter.tailor(_cv(), _posting())

    assert len(calls) == 1


# --- The per-attempt timeout and the total deadline, isolated from each other -----------------------


async def test_the_per_attempt_timeout_fires_before_a_slow_stub_completes(
    settings: Settings,
) -> None:
    async def slow_stub(prompt: str) -> RawCompletion:
        await asyncio.sleep(0.3)
        return RawCompletion(text=_valid_response_text(), prompt_tokens=1, completion_tokens=1)

    adapter = GeminiLlm(
        _settings(
            settings,
            llm_request_timeout_seconds=0.05,
            llm_max_attempts=1,
            llm_total_deadline_seconds=5,
        ),
        generate=slow_stub,
    )

    with pytest.raises(LlmTimedOut):
        await adapter.tailor(_cv(), _posting())


async def test_the_total_deadline_fires_even_though_no_single_attempt_times_out(
    settings: Settings,
) -> None:
    """Isolates the OUTER `asyncio.wait_for(..., timeout=llm_total_deadline_seconds)` from the inner,
    per-attempt one: the per-attempt bound below (5 s) never trips on its own, so a `LlmTimedOut`
    here can only be the total deadline firing while the one permitted attempt is still in flight."""

    async def slow_stub(prompt: str) -> RawCompletion:
        await asyncio.sleep(0.2)
        return RawCompletion(text=_valid_response_text(), prompt_tokens=1, completion_tokens=1)

    adapter = GeminiLlm(
        _settings(
            settings,
            llm_request_timeout_seconds=5,
            llm_max_attempts=1,
            llm_total_deadline_seconds=0.05,
        ),
        generate=slow_stub,
    )

    with pytest.raises(LlmTimedOut):
        await adapter.tailor(_cv(), _posting())


# --- AC-23: the floor, and its two privacy guarantees ------------------------------------------------


async def test_an_unrecognised_exception_becomes_llm_error_with_nothing_leaked(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    secret_fragment = "SECRET-CV-FRAGMENT-must-never-reach-a-log-line-93kd"

    async def boom(prompt: str) -> RawCompletion:
        raise KeyError(secret_fragment)

    adapter = GeminiLlm(_settings(settings, llm_max_attempts=1), generate=boom)

    with caplog.at_level(logging.INFO), pytest.raises(LlmError) as exc_info:
        await adapter.tailor(_cv(), _posting())

    # The floor's promise: `Exception`, never `BaseException`, translated to a NAMED domain error —
    # never a bare `KeyError` escaping `LlmPort.tailor`.
    assert exc_info.value.__cause__ is None, "raise ... from None must leave __cause__ unset"
    assert exc_info.value.__suppress_context__ is True

    # The privacy half: the fully-qualified TYPE is loggable; the MESSAGE — which here carries the
    # whole secret — must never travel anywhere near a log line.
    assert secret_fragment not in caplog.text
    assert "KeyError" in caplog.text, "the exception's fully-qualified type should be logged"


async def test_cancelled_error_during_a_call_is_not_swallowed(settings: Settings) -> None:
    """G-24: `except Exception`, never `except BaseException`, in the adapter's floor — a worker
    shutdown must still be able to cancel a run in flight."""

    async def slow_stub(prompt: str) -> RawCompletion:
        await asyncio.sleep(5)
        return RawCompletion(text=_valid_response_text(), prompt_tokens=1, completion_tokens=1)

    adapter = GeminiLlm(
        _settings(settings, llm_total_deadline_seconds=30, llm_request_timeout_seconds=30),
        generate=slow_stub,
    )

    task = asyncio.ensure_future(adapter.tailor(_cv(), _posting()))
    await asyncio.sleep(0.05)  # let the stub actually start before cancelling it
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --- G-22: the CV-too-large pre-flight refusal, before any API call ---------------------------------


async def test_a_cv_over_the_character_limit_is_refused_with_zero_calls(settings: Settings) -> None:
    stub, calls = _recording_stub()
    adapter = GeminiLlm(_settings(settings, llm_max_cv_characters=100), generate=stub)

    with pytest.raises(LlmInputsTooLarge):
        await adapter.tailor(_cv("word " * 200), _posting())  # 800 non-whitespace chars > 100

    assert calls == [], "G-22: reject, never call — the model never sees an over-long CV"


# --- G-32: the no-key refusal in dev/test, before any API call --------------------------------------


async def test_an_empty_key_refuses_with_zero_calls_even_with_a_stub_injected(
    settings: Settings,
) -> None:
    """The no-key check runs even when `generate` is a stub — `GeminiLlm`'s own docstring explains
    why: putting the test and production paths on different branches of this check would make "no
    test calls the real API" rest on a key nothing in this test actually reads."""
    stub, calls = _recording_stub()
    keyless = settings.model_copy(update={"gemini_api_key": ""})
    adapter = GeminiLlm(keyless, generate=stub)

    with pytest.raises(LlmUnavailable):
        await adapter.tailor(_cv(), _posting())

    assert calls == []


# --- AC-24: the prompt carries the CV and the posting, and nothing else -----------------------------


async def test_the_prompt_carries_the_cv_and_the_posting_and_nothing_else(
    settings: Settings,
) -> None:
    captured: list[str] = []

    async def stub(prompt: str) -> RawCompletion:
        captured.append(prompt)
        return RawCompletion(text=_valid_response_text(), prompt_tokens=1, completion_tokens=1)

    cv_sentinel = "CV-SENTINEL-Jane-Q-Doe-employment-history-8271"
    posting_sentinel = "POSTING-SENTINEL-Northwind-Logistics-role-4462"
    # Sentinels for the values `build_tailoring_prompt`'s signature structurally cannot receive
    # (it takes only two strings) — asserted absent anyway, as the positive proof that this
    # signature is what actually keeps them out rather than a coincidence of what this test sent in.
    email_sentinel = "careless-leak@example.com"
    account_id_sentinel = "11111111-aaaa-4bbb-8ccc-222222222222"
    guest_session_sentinel = "33333333-dddd-4eee-8fff-444444444444"
    run_id_sentinel = "55555555-aaaa-4bbb-8ccc-666666666666"
    ip_sentinel = "203.0.113.42"

    cv = _cv(("filler word " * 60) + cv_sentinel)
    posting = _posting(("filler posting text " * 20) + posting_sentinel)
    adapter = GeminiLlm(_settings(settings), generate=stub)

    await adapter.tailor(cv, posting)

    assert len(captured) == 1
    prompt = captured[0]
    assert cv_sentinel in prompt
    assert posting_sentinel in prompt
    for absent in (
        email_sentinel,
        account_id_sentinel,
        guest_session_sentinel,
        run_id_sentinel,
        ip_sentinel,
    ):
        assert absent not in prompt


# --- The thinking decision: pinned at the config-construction seam, since the stub never sees it ----


def test_build_generate_config_disables_thinking_and_pins_the_response_schema(
    settings: Settings,
) -> None:
    """Owner decision, 2026-09-11 (carried finding from T25, applied 2026-09-12): thinking is
    disabled (`thinking_config=ThinkingConfig(thinking_budget=0)`) because thinking tokens count
    against `llm_max_output_tokens` — `gemini-2.5-flash` thinks by default, so with thinking left on
    the model could spend the whole cap thinking and return truncated JSON on EVERY run, which this
    adapter would faithfully record as `llm_output_invalid` and no test in `make check` could see
    (tests never call the real API). Every other test in this file drives the injected `GenerateFn`
    stub, which receives only the prompt string and never constructs or inspects this object — so
    this is the ONLY test in the suite that can pin the decision at all.
    """
    llm_settings = _settings(settings, llm_max_output_tokens=4096)

    config = build_generate_config(llm_settings)

    assert config.thinking_config is not None
    assert config.thinking_config.thinking_budget == 0
    assert config.response_mime_type == "application/json"
    assert config.max_output_tokens == 4096

    schema = config.response_schema
    assert isinstance(schema, genai_types.Schema)
    assert schema.required == [KEY_TAILORED_CV, KEY_COVER_LETTER]
    assert schema.properties is not None
    assert schema.properties[KEY_TAILORED_CV].type == genai_types.Type.STRING
    assert schema.properties[KEY_COVER_LETTER].type == genai_types.Type.STRING


# --- Wiring: production builds the real client, never a stub ----------------------------------------


def test_the_production_wiring_binds_the_real_sdk_client(settings: Settings) -> None:
    llm = get_llm(_settings(settings))

    assert isinstance(llm, GeminiLlm)
    assert llm._generate == llm._generate_with_sdk, (
        "production must never construct GeminiLlm with a stub `generate` — the strict default is "
        "the only seam that keeps 'no test calls the real Gemini API' true structurally"
    )


# --- The SDK's own logging: a finding, not a silencing (this task's explicit instruction) -----------


class _GeminiStubServer:
    """A minimal local HTTP server standing in for Gemini's endpoint, so the REAL `genai.Client` can
    be driven with no network and no real key. Deliberately much smaller than
    `test_posting_fetcher.py`'s `_StubServer` — this file needs exactly one canned JSON response, not
    redirects, gzip or paced bodies."""

    def __init__(self, response_body: bytes, status: int = 200) -> None:
        handler_cls = self._make_handler(response_body, status)
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @staticmethod
    def _make_handler(body: bytes, status: int) -> type[http.server.BaseHTTPRequestHandler]:
        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)  # drained, never inspected — this test is about LOGGING
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass  # silence the stub's own access log — noise in `make test` output

        return _Handler

    @property
    def base_url(self) -> str:
        port = self._httpd.server_address[1]
        return f"http://127.0.0.1:{port}"

    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


async def test_the_real_sdk_client_logs_nothing_derived_from_the_prompt_or_the_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T34's mandated check: 1.2's `/verify` found `httpx` logging full URLs at INFO one frame below
    a clean adapter, invisible to a test that drove a fake — so this one drives the REAL
    `genai.Client` (not the injected `GenerateFn` stub every other test above uses) against a local
    stub server, and inspects what the vendor library itself puts in the log.

    **Findings** (reported here rather than silenced, per this task's explicit instruction not to add
    a logger unless it actually leaks something):

    - `google_genai.models` — the module the real async `generate_content` call runs through —
      logs two lines on EVERY call, at INFO and WARNING: `"AFC is enabled with max remote calls: N."`
      and a one-time notice steering callers toward `AsyncChat` for automatic function calling. Both
      are static vendor text with no argument interpolated from the prompt, the config or the
      response — confirmed by running the real call below and asserting the sentinels are absent.
      Note the module name is `google_genai` (underscore), not `google.genai` (dot) — the two are
      different strings to `logging.getLogger`, and only the underscore form is what the installed
      SDK actually calls.
    - `httpx` logs one INFO line per request (`"HTTP Request: POST <url> ... "<status>""`) — already
      in `_SILENCED_VENDOR_LOGGERS` since slice 1.2. The URL carries the model name and API version,
      never the API key (sent as a header, per the SDK) and never the prompt.
    - `httpcore` logs at DEBUG only, and only connection/header lifecycle events (`connect_tcp.*`,
      `send_request_headers.*`, …) — never a request or response body, at any level.

    **Conclusion: nothing here needs to be added to `_SILENCED_VENDOR_LOGGERS`.** `httpx` already
    covers the one vendor logger that emits per-call operational noise close to the wire, and neither
    it nor `google_genai.models` ever puts a fragment of the prompt or the completion in a log
    record — verified directly, not assumed.
    """
    cv_sentinel = "SDK-LOG-SENTINEL-employment-history-must-not-leak-771x"
    response_text = _valid_response_text()
    response_payload = json.dumps(
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": response_text}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 5},
        }
    ).encode()

    server = _GeminiStubServer(response_payload)
    server.start()
    try:
        client = genai.Client(
            api_key="placeholder-not-a-real-key",
            http_options=genai_types.HttpOptions(base_url=server.base_url),
        )
        with caplog.at_level(logging.DEBUG):
            response = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"tailor this CV: {cv_sentinel}",
                config=genai_types.GenerateContentConfig(response_mime_type="application/json"),
            )
    finally:
        server.shutdown()

    assert response.text == response_text  # the call actually completed, for the right reason
    assert caplog.records, "expected the SDK to have produced at least one log record"
    assert cv_sentinel not in caplog.text, "the prompt reached a log line"
    assert response_text not in caplog.text, "the completion reached a log line"
