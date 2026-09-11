"""API tests for `tailoring-generate-documents`'s HTTP surface: `POST/GET /api/tailoring-runs[/{id}]`.

**This is the RED half of a red-first cycle** (CLAUDE.md, sdlc.md §2, T30). Every test here is written
against `docs/specs/tailoring-generate-documents/feature-spec.md`'s failure contract (G-1…G-34) and
its acceptance criteria — not against `infrastructure/api/routers/tailoring.py`, whose three handlers
currently do nothing but `raise NotImplementedError` (T29's SKELETON). The router IS mounted
(`create_app` includes it), so a passing collection with every test failing on a real assertion —
never a 404, never an ImportError — is exactly what a correct RED run looks like here.

This file replaces an earlier, partial draft of the same name that was interrupted mid-write. Nothing
from that draft was trusted on faith: every test kept here was re-derived from the spec and re-checked
against the router/schema skeletons and the domain code before being written down.

**Three findings shape every test below, and each is load-bearing:**

1. **`app.dependency_overrides[deps.get_llm]` does not reach the worker.** No API route resolves
   `LlmPort` — ADR-0014's whole design is that the request commits a `queued` run and publishes; the
   *Celery task* calls the model, and it builds its own graph through
   `infrastructure/tasks/container.py::_build_use_case`, a **second composition root**
   `dependency_overrides` cannot touch. `_install_worker_llm` below monkeypatches the name `GeminiLlm`
   as imported into that module, and every test that drives the worker asserts on `fake_llm.calls` —
   the fake's use is **asserted**, never inferred from the test passing.
2. **With no cookie, `require_guest_session` fires before FastAPI's own body/path validation.** A
   malformed-UUID request with no cookie answers 401 `guest_session_expired`, not 422. Every test below
   that asserts a 422 from `CreateTailoringRunRequest` or from the `{tailoring_run_id}` path parameter
   therefore mints a real session first (`_mint_cookie`), or it would be asserting against the wrong
   row of the contract.
3. **A database transaction cannot see another connection's uncommitted work.** `conftest.py` isolates
   every test inside one outer transaction that is rolled back at teardown and never truly committed at
   the Postgres wire level. `infrastructure/tasks/container.py::tailoring_use_case` builds a
   *brand-new* engine and connection when it runs for real — correct in production, and unusable here:
   a second, genuinely separate connection cannot see a run this test's own `POST` just wrote, no
   matter what SQLAlchemy calls the savepoint that "committed" it. `_run_worker` below calls
   `container._build_use_case(settings, session)` directly — the same function production calls, with
   the worker's LLM replaced — bound to *this test's own* `session` instead of a second connection.
   That is what makes "run the task" mean anything inside this suite at all.

**No test in this suite makes a real call to Gemini.** Two independent guarantees, not one: test
settings carry an empty `gemini_api_key`, so even an unpatched `GeminiLlm` would refuse with
`LlmUnavailable` before any request left the process (G-32) — and on top of that, every test that
drives the worker replaces `GeminiLlm` outright via `_install_worker_llm`, so "no test calls the real
API" does not rest on a setting nobody here reads twice.

**Base CVs and job postings are seeded through the real HTTP API**, not hand-built through a
repository. Both endpoints are fully implemented (slices 1.1 and 1.2), extraction is synchronous, and a
`.txt` upload gives a deterministic `status: "extracted"` in the same response — so `_ready_inputs` is
a normal user flow rather than a fixture shortcut, and the base-CV/job-posting authorization checks
this slice **reuses** (`GetBaseCvForSession`, `GetJobPostingForSession`) are exercised for real.

**Why this file's `client` fixture is not the shared one, and why `clear_redis` rides every test that
posts** — both for the identical reasons `test_intake.py` and `test_posting.py` document at length:
`ASGITransport`'s default `raise_app_exceptions=True` turns a skeleton's `NotImplementedError` into a
Python exception rather than a real `500` response, burying the assertion this file exists to make; and
the `tailoring:create` Redis namespace is untouched by the per-test database rollback, so a leftover
counter from an earlier test silently changes a later one's rate-limit assertions. Applied here as an
autouse fixture, exactly as the other two API test modules do it.

**AC-21's privacy capture must see the worker's log records, not only the API's.** T28 wired the
worker's own logging configuration through `celeryd_init`/`worker_process_init`, but those Celery
signals never fire here — `_run_worker` calls `container._build_use_case` directly, in-process, never
through a real worker startup. That is not a gap: `create_app(settings)` already calls
`configure_logging(settings)` synchronously (not inside the lifespan `ASGITransport` skips), once per
test via the `app` fixture, so `structlog` is already wired process-wide by the time `_run_worker`'s
loggers emit — they reach the same `logging.Logger` instances `caplog` already listens to. **Verified
positively, not merely by a non-empty guard**: `_install_worker_llm` replaces `GeminiLlm` with a fake
that logs nothing, and `_run_worker` never goes through the Celery task's own log line either, so the
*only* tailoring-specific records this suite can see are `LoggingEventPublisher`'s `domain_event`
lines. `assert caplog.records` alone would pass on any record at all — SQLAlchemy, httpx, uvicorn —
which proves nothing about whether the tailoring channel was ever captured. The privacy test instead
asserts each run's own `tailoring_run_id` (which AC-21 requires to be logged) appears in the captured
text, which is the positive proof that the channel this test's negative assertions depend on was
actually seen.

**Deliberately not covered in this file, and why:**

- **G-24** (`asyncio.CancelledError` inside the adapter is not translated) is an adapter-internal
  behaviour with no HTTP-visible effect to assert against; it is pinned at the adapter level (T34, the
  Gemini adapter's own tests), exactly as the spec's test plan puts it there.
- **G-28** (Postgres unavailable inside the worker while recording success) requires killing the
  database mid-transaction from inside a use case this suite's own transactional fixture depends on to
  exist at all — the spec names it as an accepted residual reachable only by a real outage, not
  something an API test can safely induce without corrupting its own isolation.
- **G-32** (empty `GEMINI_API_KEY` in production) and **G-33** (markup in the model's output rendered
  as text) are a startup-time settings guard and a frontend rendering rule respectively; neither has an
  HTTP-layer assertion — they belong to `Settings`'s own tests and to the Vitest suite (T41).
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text as sql_text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.tailoring.execute_tailoring_run import (
    ExecuteTailoringRunCommand,
    ExecuteTailoringRunOutcome,
)
from tailorcraft.domain.tailoring.errors import (
    LlmError,
    LlmInputsTooLarge,
    LlmOutputInvalid,
    LlmRateLimited,
    LlmRefused,
    LlmTimedOut,
    LlmUnavailable,
    TailoringFailed,
    TailoringNotQueued,
)
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocuments,
    TailoredDraft,
    TailoringRunId,
)
from tailorcraft.infrastructure.api.deps import get_app_settings, get_clock, get_tailoring_queue
from tailorcraft.infrastructure.api.guest_session import COOKIE_NAME
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container as tailoring_container
from tests.integration.fakes import FakeLlm, FakeTailoringQueue

# ---------------------------------------------------------------------------------------------
# Fixture text — comfortably past every floor this slice's inputs and outputs must clear, and
# distinctive enough that the AC-21 privacy test can catch a *partial* leak, not only a total one.
# ---------------------------------------------------------------------------------------------


def _cv_body_text(marker: str = "QA_FIXTURE_CV_TOKEN") -> str:
    """Plain text well past `ExtractedText`'s 200-non-whitespace-character floor, uploaded as a `.txt`
    base CV so extraction is synchronous and deterministic — no PDF/DOCX parsing needed."""
    filler = "Senior backend engineer with a decade leading platform reliability work. " * 8
    return f"{marker} {filler}"


def _posting_body_text(marker: str = "QA_FIXTURE_POSTING_TOKEN") -> str:
    """Past `JobPostingText`'s 100-non-whitespace-character floor, under its 30,000 ceiling."""
    filler = "We are hiring a senior engineer to own reliability and mentor the team. " * 4
    return f"{marker} {filler}"


def _a_draft(
    cv_marker: str = "QA_FIXTURE_TAILORED_CV_TOKEN",
    letter_marker: str = "QA_FIXTURE_TAILORED_LETTER_TOKEN",
) -> TailoredDraft:
    """A well-formed `TailoredDraft`, comfortably past both documents' floors (400 / 200 non-whitespace
    characters) and comfortably under both ceilings (20,000 / 8,000)."""
    cv_filler = "Rewrote the platform reliability program end to end for this role. " * 8
    letter_filler = "I am applying because this role matches my reliability background. " * 6
    return TailoredDraft(
        documents=TailoredDocuments(
            cv=TailoredCv(f"{cv_marker} {cv_filler}"),
            cover_letter=CoverLetter(f"{letter_marker} {letter_filler}"),
        ),
        metrics=LlmCallMetrics(
            model=ModelName("gemini-test"),
            prompt_version=PromptVersion("1"),
            prompt_tokens=111,
            completion_tokens=222,
            duration_ms=1234,
        ),
    )


# ---------------------------------------------------------------------------------------------
# Small HTTP helpers — mirror test_posting.py's and test_intake.py's
# ---------------------------------------------------------------------------------------------


def _error_code(response: Response) -> str:
    body = response.json()
    assert "error" in body, f"expected the {{'error': {{...}}}} envelope, got {body!r}"
    return str(body["error"]["code"])


def _guest_cookie_header(response: Response) -> str | None:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{COOKIE_NAME}="):
            return header
    return None


def _override_settings(app: FastAPI, base: Settings, **updates: object) -> Settings:
    """Point every settings-reading path at one modified `Settings` — see `test_posting.py`'s twin."""
    modified = base.model_copy(update=updates)
    app.dependency_overrides[get_app_settings] = lambda: modified
    app.state.settings = modified
    return modified


def _new_client(app: FastAPI) -> AsyncClient:
    """A second, independent cookie jar against the same app — for tests that need a second, distinct
    guest session (the authorization rows, G-6/G-7/G-29) or a shared client IP (G-11's per-IP limit)."""
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
    )


# ---------------------------------------------------------------------------------------------
# Seeding — through the real, already-shipped `intake`/`posting` HTTP surfaces
# ---------------------------------------------------------------------------------------------


async def _mint_cookie(client: AsyncClient) -> None:
    """A valid guest-session cookie, needed before hitting a `require_guest_session`-gated validation
    error: with no cookie, that dependency raises 401 before FastAPI's own body/path validation ever
    runs (this module's second finding), so a G-1/G-2/G-30-style test needs a real session or it
    asserts against the wrong row."""
    response = await client.post(
        "/api/base-cvs", files={"file": ("mint.txt", _cv_body_text("MINT").encode(), "text/plain")}
    )
    assert response.status_code == 201, response.text


async def _upload_ready_base_cv(client: AsyncClient, *, text: str | None = None) -> str:
    """Upload a `.txt` CV and return its id. Extraction is synchronous (1.1): the response already
    carries `status: "extracted"`, so there is nothing to poll."""
    body_text = text if text is not None else _cv_body_text()
    response = await client.post(
        "/api/base-cvs", files={"file": ("cv.txt", body_text.encode(), "text/plain")}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extracted", body
    return str(body["id"])


async def _upload_unextracted_base_cv(client: AsyncClient) -> str:
    """A base CV that exists, is owned by the caller, and is **not** `extracted` (G-8): a `.txt` upload
    short enough to miss `ExtractedText`'s 200-non-whitespace-character floor lands in
    `extraction_failed`, not `extracted` — the same mechanism `test_intake.py`'s
    `test_extraction_below_the_200_character_floor_returns_too_short` exercises."""
    response = await client.post(
        "/api/base-cvs", files={"file": ("tiny.txt", b"too short", "text/plain")}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extraction_failed", body
    return str(body["id"])


async def _create_ready_job_posting(client: AsyncClient, *, text: str | None = None) -> str:
    body_text = text if text is not None else _posting_body_text()
    response = await client.post("/api/job-postings", json={"source": "pasted", "text": body_text})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _ready_inputs(
    client: AsyncClient, *, cv_text: str | None = None, posting_text: str | None = None
) -> tuple[str, str]:
    """A base CV (`extracted`) and a job posting, both owned by `client`'s session, ready to name in a
    `POST /api/tailoring-runs` body."""
    base_cv_id = await _upload_ready_base_cv(client, text=cv_text)
    job_posting_id = await _create_ready_job_posting(client, text=posting_text)
    return base_cv_id, job_posting_id


async def _create_queued_run(
    client: AsyncClient,
    app: FastAPI,
    *,
    cv_text: str | None = None,
    posting_text: str | None = None,
) -> str:
    """`_ready_inputs` plus the `POST` itself, with a non-raising queue installed. Returns the new
    run's id, left `queued` (the worker is never run)."""
    _install_queue(app)
    base_cv_id, job_posting_id = await _ready_inputs(
        client, cv_text=cv_text, posting_text=posting_text
    )
    response = await client.post(
        "/api/tailoring-runs", json={"base_cv_id": base_cv_id, "job_posting_id": job_posting_id}
    )
    assert response.status_code == 202, response.text
    return str(response.json()["id"])


# ---------------------------------------------------------------------------------------------
# The queue — never a real broker in this suite
# ---------------------------------------------------------------------------------------------


def _install_queue(app: FastAPI) -> FakeTailoringQueue:
    queue = FakeTailoringQueue()
    app.dependency_overrides[get_tailoring_queue] = lambda: queue
    return queue


def _install_failing_queue(app: FastAPI) -> FakeTailoringQueue:
    """G-14: the broker refuses the publish after the run row is already committed."""
    queue = FakeTailoringQueue(outcome=TailoringNotQueued("simulated broker failure (G-14)"))
    app.dependency_overrides[get_tailoring_queue] = lambda: queue
    return queue


# ---------------------------------------------------------------------------------------------
# The worker's LLM and clock — replaced on the WORKER's composition root, never on `deps.py`'s (this
# module's first finding: no API route resolves `LlmPort` at all).
# ---------------------------------------------------------------------------------------------


def _install_worker_llm(monkeypatch: pytest.MonkeyPatch, fake_llm: FakeLlm) -> None:
    """Redirect every call `container._build_use_case` makes for `LlmPort` to `fake_llm`, for the life
    of this test. `app.dependency_overrides[deps.get_llm]` cannot do this — see the module docstring's
    first finding — so this patches the name `GeminiLlm` as imported into
    `infrastructure/tasks/container.py`, the worker's own composition root."""
    monkeypatch.setattr(tailoring_container, "GeminiLlm", lambda settings: fake_llm)


def _install_worker_clock(monkeypatch: pytest.MonkeyPatch, clock: FixedClock) -> None:
    """Replace `SystemClock` on the worker's composition root — needed only by G-25's `abandoned` row,
    which must observe "now" past a 300-second stale window without a real wait."""
    monkeypatch.setattr(tailoring_container, "SystemClock", lambda: clock)


async def _run_worker(
    session: AsyncSession, settings: Settings, run_id: str
) -> ExecuteTailoringRunOutcome:
    """Execute one queued run through the REAL `ExecuteTailoringRun`, via the REAL
    `container._build_use_case` — the same function production calls — bound to *this test's own*
    session rather than a second, genuinely separate connection.

    `container.tailoring_use_case()` builds a brand-new engine and connection when it runs for real,
    which is correct in production and unusable here — see the module docstring's third finding: a
    second connection cannot see a transaction this suite's own `conftest.py` never actually commits at
    the Postgres wire level. Calling `_build_use_case` directly is what makes the run this test just
    `POST`ed visible to the use case about to execute it, while still exercising the same production
    wiring code (repositories, `CommittingTailoringRunRepository`, the LLM binding) rather than a
    hand-rolled substitute.
    """
    execute = tailoring_container._build_use_case(settings, session)
    return await execute(ExecuteTailoringRunCommand(tailoring_run_id=TailoringRunId(UUID(run_id))))


async def _count_guest_sessions(session: AsyncSession) -> int:
    result = await session.execute(sql_text("SELECT count(*) FROM identity_guest_session"))
    return int(result.scalar_one())


async def _count_tailoring_runs(session: AsyncSession) -> int:
    result = await session.execute(sql_text("SELECT count(*) FROM tailoring_run"))
    return int(result.scalar_one())


# ---------------------------------------------------------------------------------------------
# Module-local fixtures
# ---------------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Shadows `conftest.py`'s `client` fixture — see the module docstring for why
    `raise_app_exceptions=False` matters here."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """Applies `clear_redis` (conftest.py) to every test in this module. `tailoring:create` is not
    touched by the per-test transaction rollback (CLAUDE.md)."""
    return None


# ---------------------------------------------------------------------------------------------
# G-1 — body is not JSON, or is empty (requires a valid cookie — finding #2)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_body", [pytest.param(b"", id="empty"), pytest.param(b"not json at all", id="not-json")]
)
async def test_body_that_is_not_valid_json_returns_422_validation_error(
    client: AsyncClient, raw_body: bytes
) -> None:
    await _mint_cookie(client)

    response = await client.post(
        "/api/tailoring-runs", content=raw_body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


# ---------------------------------------------------------------------------------------------
# G-2 — an id missing or not a UUID (requires a valid cookie — finding #2)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"job_posting_id": str(uuid4())}, id="base_cv_id-missing"),
        pytest.param({"base_cv_id": str(uuid4())}, id="job_posting_id-missing"),
        pytest.param(
            {"base_cv_id": "not-a-uuid", "job_posting_id": str(uuid4())}, id="base_cv_id-malformed"
        ),
        pytest.param(
            {"base_cv_id": str(uuid4()), "job_posting_id": "not-a-uuid"},
            id="job_posting_id-malformed",
        ),
    ],
)
async def test_missing_or_malformed_id_returns_422_validation_error(
    client: AsyncClient, body: dict[str, object]
) -> None:
    await _mint_cookie(client)

    response = await client.post("/api/tailoring-runs", json=body)

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


# ---------------------------------------------------------------------------------------------
# G-3 — the 256 KiB JSON body cap, refused before parsing (no cookie needed: the middleware runs
# before dependency resolution, exactly as `test_posting.py`'s identical P-7 test does not mint one)
# ---------------------------------------------------------------------------------------------


async def test_json_body_over_the_cap_returns_413_request_too_large(
    client: AsyncClient, settings: Settings
) -> None:
    padding = "a" * (settings.json_request_max_bytes + 1024)
    body = (
        '{"base_cv_id":"'
        + str(uuid4())
        + '","job_posting_id":"'
        + str(uuid4())
        + '","padding":"'
        + padding
        + '"}'
    ).encode()
    assert len(body) > settings.json_request_max_bytes

    response = await client.post(
        "/api/tailoring-runs", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 413, response.text
    assert _error_code(response) == "request_too_large"


# ---------------------------------------------------------------------------------------------
# G-4 / AC-16 — no guest cookie: 401, no Set-Cookie, no new `identity_guest_session` row
# ---------------------------------------------------------------------------------------------


async def test_cookieless_post_returns_401_mints_no_session_and_creates_no_row(
    client: AsyncClient, session: AsyncSession
) -> None:
    before = await _count_guest_sessions(session)

    response = await client.post(
        "/api/tailoring-runs", json={"base_cv_id": str(uuid4()), "job_posting_id": str(uuid4())}
    )

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"
    assert _guest_cookie_header(response) is None, "AC-16: this POST must never mint a session"
    after = await _count_guest_sessions(session)
    assert after == before, "AC-16: a cookieless POST must create no identity_guest_session row"


# ---------------------------------------------------------------------------------------------
# G-5 / G-31 — unknown or expired cookie: 401 on POST and on both GETs
# ---------------------------------------------------------------------------------------------


async def test_unknown_cookie_on_post_returns_401(client: AsyncClient) -> None:
    client.cookies.set(COOKIE_NAME, "a-token-that-was-never-minted-by-this-server")

    response = await client.post(
        "/api/tailoring-runs", json={"base_cv_id": str(uuid4()), "job_posting_id": str(uuid4())}
    )

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


async def test_expired_cookie_on_post_returns_401(client: AsyncClient, app: FastAPI) -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    app.dependency_overrides[get_clock] = lambda: clock
    await _mint_cookie(client)

    clock.advance(25 * 3600)  # past the default 24h retention window

    response = await client.post(
        "/api/tailoring-runs", json={"base_cv_id": str(uuid4()), "job_posting_id": str(uuid4())}
    )

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


async def test_get_list_with_no_cookie_returns_401(client: AsyncClient) -> None:
    response = await client.get("/api/tailoring-runs")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


async def test_get_one_with_no_cookie_returns_401(client: AsyncClient) -> None:
    response = await client.get(f"/api/tailoring-runs/{uuid4()}")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


async def test_get_list_with_unknown_cookie_returns_401(client: AsyncClient) -> None:
    client.cookies.set(COOKIE_NAME, "a-token-that-was-never-minted-by-this-server")

    response = await client.get("/api/tailoring-runs")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


async def test_get_one_with_expired_cookie_returns_401(client: AsyncClient, app: FastAPI) -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    app.dependency_overrides[get_clock] = lambda: clock
    await _mint_cookie(client)

    clock.advance(25 * 3600)

    response = await client.get(f"/api/tailoring-runs/{uuid4()}")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


# ---------------------------------------------------------------------------------------------
# AC-1 — the 202 body, with every terminal field null, and the Location header
# ---------------------------------------------------------------------------------------------


async def test_successful_post_returns_202_queued_with_null_fields_and_location_header(
    client: AsyncClient, app: FastAPI
) -> None:
    base_cv_id, job_posting_id = await _ready_inputs(client)
    _install_queue(app)

    response = await client.post(
        "/api/tailoring-runs", json={"base_cv_id": base_cv_id, "job_posting_id": job_posting_id}
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["base_cv_id"] == base_cv_id
    assert body["job_posting_id"] == job_posting_id
    assert body["tailored_cv"] is None
    assert body["cover_letter"] is None
    assert body["model"] is None
    assert body["completed_at"] is None
    assert body["failure_reason"] is None
    assert response.headers["location"] == f"/api/tailoring-runs/{body['id']}"


# ---------------------------------------------------------------------------------------------
# AC-2 / AC-20(a) — the whole path, end to end, with an instant fake LLM, under the 2s plumbing budget
# ---------------------------------------------------------------------------------------------


async def test_full_happy_path_end_to_end_succeeds_under_two_seconds(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_llm = FakeLlm(_a_draft())
    _install_worker_llm(monkeypatch, fake_llm)
    _install_queue(app)
    base_cv_id, job_posting_id = await _ready_inputs(client)

    started = time.perf_counter()
    create_response = await client.post(
        "/api/tailoring-runs", json={"base_cv_id": base_cv_id, "job_posting_id": job_posting_id}
    )
    assert create_response.status_code == 202, create_response.text
    run_id = create_response.json()["id"]

    outcome = await _run_worker(session, settings, run_id)
    assert outcome is ExecuteTailoringRunOutcome.SUCCEEDED

    get_response = await client.get(f"/api/tailoring-runs/{run_id}")
    elapsed = time.perf_counter() - started

    assert get_response.status_code == 200, get_response.text
    body = get_response.json()
    assert body["status"] == "succeeded"
    assert body["failure_reason"] is None
    assert "QA_FIXTURE_TAILORED_CV_TOKEN" in body["tailored_cv"]
    assert "QA_FIXTURE_TAILORED_LETTER_TOKEN" in body["cover_letter"]
    assert body["model"] == "gemini-test"
    assert body["prompt_version"] == "1"
    assert body["llm_duration_ms"] == 1234
    assert len(fake_llm.calls) == 1, (
        "the fake's use is asserted, never inferred from the test passing"
    )
    assert elapsed < 2.0, (
        f"plumbing took {elapsed:.3f}s (AC-20(a) budget: under 2s with an instant fake)"
    )


# ---------------------------------------------------------------------------------------------
# G-6 — base_cv_id does not exist, or belongs to another session: 404 base_cv_not_found
# ---------------------------------------------------------------------------------------------


async def test_base_cv_id_nonexistent_or_owned_by_another_session_returns_404(
    client: AsyncClient, app: FastAPI
) -> None:
    _own_base_cv_id, job_posting_id = await _ready_inputs(client)
    async with _new_client(app) as other_client:
        foreign_base_cv_id = await _upload_ready_base_cv(other_client)

    for bad_base_cv_id in (str(uuid4()), foreign_base_cv_id):
        response = await client.post(
            "/api/tailoring-runs",
            json={"base_cv_id": bad_base_cv_id, "job_posting_id": job_posting_id},
        )
        assert response.status_code == 404, response.text
        assert _error_code(response) == "base_cv_not_found"


# ---------------------------------------------------------------------------------------------
# G-7 — job_posting_id does not exist, or belongs to another session: 404 job_posting_not_found
# ---------------------------------------------------------------------------------------------


async def test_job_posting_id_nonexistent_or_owned_by_another_session_returns_404(
    client: AsyncClient, app: FastAPI
) -> None:
    base_cv_id = await _upload_ready_base_cv(client)
    async with _new_client(app) as other_client:
        foreign_job_posting_id = await _create_ready_job_posting(other_client)

    for bad_job_posting_id in (str(uuid4()), foreign_job_posting_id):
        response = await client.post(
            "/api/tailoring-runs",
            json={"base_cv_id": base_cv_id, "job_posting_id": bad_job_posting_id},
        )
        assert response.status_code == 404, response.text
        assert _error_code(response) == "job_posting_not_found"


# ---------------------------------------------------------------------------------------------
# G-8 — base CV owned but not `extracted`: 409 base_cv_not_extracted, no row created
# ---------------------------------------------------------------------------------------------


async def test_base_cv_not_extracted_returns_409_and_creates_no_row(
    client: AsyncClient, session: AsyncSession
) -> None:
    base_cv_id = await _upload_unextracted_base_cv(client)
    job_posting_id = await _create_ready_job_posting(client)
    before = await _count_tailoring_runs(session)

    response = await client.post(
        "/api/tailoring-runs", json={"base_cv_id": base_cv_id, "job_posting_id": job_posting_id}
    )

    assert response.status_code == 409, response.text
    assert _error_code(response) == "base_cv_not_extracted"
    after = await _count_tailoring_runs(session)
    assert after == before, "AC/OQ-2: a rejected POST before the enqueue must create no row"


# ---------------------------------------------------------------------------------------------
# G-9 / G-34 — an active run already exists: 409 tailoring_already_running, carrying its id. A
# genuine double-click looks exactly like this second request.
# ---------------------------------------------------------------------------------------------


async def test_second_post_while_a_run_is_active_returns_409_with_the_active_run_id(
    client: AsyncClient, app: FastAPI
) -> None:
    base_cv_id, job_posting_id = await _ready_inputs(client)
    _install_queue(app)
    body = {"base_cv_id": base_cv_id, "job_posting_id": job_posting_id}

    first = await client.post("/api/tailoring-runs", json=body)
    assert first.status_code == 202, first.text
    first_id = first.json()["id"]

    second = await client.post("/api/tailoring-runs", json=body)

    assert second.status_code == 409, second.text
    assert _error_code(second) == "tailoring_already_running"
    assert second.json()["error"]["active_tailoring_run_id"] == first_id


# ---------------------------------------------------------------------------------------------
# G-10 — the per-session run cap: 409 too_many_tailoring_runs
# ---------------------------------------------------------------------------------------------


async def test_exceeding_the_per_session_run_cap_returns_409_too_many_tailoring_runs(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modified = _override_settings(app, settings, max_tailoring_runs_per_session=2)
    base_cv_id, job_posting_id = await _ready_inputs(client)
    body = {"base_cv_id": base_cv_id, "job_posting_id": job_posting_id}

    for _ in range(modified.max_tailoring_runs_per_session):
        fake_llm = FakeLlm(_a_draft())
        _install_worker_llm(monkeypatch, fake_llm)
        _install_queue(app)
        response = await client.post("/api/tailoring-runs", json=body)
        assert response.status_code == 202, response.text
        # Decide each run before creating the next, so G-9's active-run guard does not mask G-10's.
        outcome = await _run_worker(session, settings, response.json()["id"])
        assert outcome is ExecuteTailoringRunOutcome.SUCCEEDED
        assert len(fake_llm.calls) == 1, (
            "the fake's use is asserted, never inferred from the test passing"
        )

    _install_queue(app)
    over_the_cap = await client.post("/api/tailoring-runs", json=body)

    assert over_the_cap.status_code == 409, over_the_cap.text
    assert _error_code(over_the_cap) == "too_many_tailoring_runs"


# ---------------------------------------------------------------------------------------------
# G-11 / AC-17 — rate limiting: per-session and per-client-IP, 429 + Retry-After
# ---------------------------------------------------------------------------------------------


async def test_more_than_the_per_session_rate_limit_returns_429(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    _override_settings(app, settings, tailoring_rate_limit_per_hour=2)
    _install_queue(app)
    base_cv_id, job_posting_id = await _ready_inputs(client)
    body = {"base_cv_id": base_cv_id, "job_posting_id": job_posting_id}

    first = await client.post("/api/tailoring-runs", json=body)
    second = await client.post("/api/tailoring-runs", json=body)
    third = await client.post("/api/tailoring-runs", json=body)

    # The second request may legitimately be a 409 (G-9's active-run guard) rather than a 202 — what
    # this test asserts is that the *rate limit itself* has not fired before the budget is spent, and
    # that it has by the third request regardless of the other rules in play.
    assert first.status_code != 429, first.text
    assert second.status_code != 429, second.text
    assert third.status_code == 429, third.text
    assert _error_code(third) == "rate_limited"
    assert "retry-after" in {name.lower() for name in third.headers}


async def test_more_than_the_per_ip_rate_limit_returns_429(
    app: FastAPI, settings: Settings
) -> None:
    """Each request mints its *own* fresh guest session (a new client, no cookie), so only the shared,
    fixed client IP (`ASGITransport`'s default peer) accumulates across them — isolating the per-IP
    limit from the per-session one, exactly as `test_posting.py`'s twin does."""
    _override_settings(app, settings, tailoring_rate_limit_per_ip_per_hour=2)

    async def _attempt() -> Response:
        async with _new_client(app) as one_shot_client:
            base_cv_id, job_posting_id = await _ready_inputs(one_shot_client)
            _install_queue(app)
            return await one_shot_client.post(
                "/api/tailoring-runs",
                json={"base_cv_id": base_cv_id, "job_posting_id": job_posting_id},
            )

    first = await _attempt()
    second = await _attempt()
    third = await _attempt()

    assert first.status_code != 429, first.text
    assert second.status_code != 429, second.text
    assert third.status_code == 429, third.text
    assert _error_code(third) == "rate_limited"


# ---------------------------------------------------------------------------------------------
# G-12 / AC-18 — Redis unreachable: the tailoring limiter fails CLOSED
# ---------------------------------------------------------------------------------------------


async def test_redis_unreachable_returns_503_creates_no_run_and_enqueues_nothing(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    queue = _install_queue(app)
    await _mint_cookie(client)
    before = await _count_tailoring_runs(session)
    _override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await client.post(
        "/api/tailoring-runs", json={"base_cv_id": str(uuid4()), "job_posting_id": str(uuid4())}
    )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "rate_limit_unavailable"
    after = await _count_tailoring_runs(session)
    assert after == before, "AC-18: no run may be created while the limiter fails closed"
    assert queue.enqueued == [], "AC-18: no task may be published while the limiter fails closed"


# ---------------------------------------------------------------------------------------------
# G-13 — the commit fails before the enqueue: 503 service_unavailable
# ---------------------------------------------------------------------------------------------


async def test_commit_failure_before_enqueue_returns_503_service_unavailable(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_cv_id, job_posting_id = await _ready_inputs(client)

    async def _raise_sqlalchemy_error() -> None:
        raise SQLAlchemyError("simulated commit failure (G-13)")

    monkeypatch.setattr(session, "commit", _raise_sqlalchemy_error)

    response = await client.post(
        "/api/tailoring-runs", json={"base_cv_id": base_cv_id, "job_posting_id": job_posting_id}
    )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"


# ---------------------------------------------------------------------------------------------
# G-14 / AC-19 — the enqueue fails after the row is committed: 503 queue_unavailable, and the row
# survives, recorded failed/not_queued
# ---------------------------------------------------------------------------------------------


async def test_broker_failure_after_commit_returns_503_and_records_failed_not_queued(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    _install_failing_queue(app)
    base_cv_id, job_posting_id = await _ready_inputs(client)
    before = await _count_tailoring_runs(session)

    response = await client.post(
        "/api/tailoring-runs", json={"base_cv_id": base_cv_id, "job_posting_id": job_posting_id}
    )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "queue_unavailable"
    after = await _count_tailoring_runs(session)
    assert after == before + 1, "AC-19: the run row must survive even though the enqueue failed"

    list_response = await client.get("/api/tailoring-runs")
    items = list_response.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "failed"
    assert items[0]["failure_reason"] == "not_queued"
    assert items[0]["retryable"] is True
    assert "tailored_cv" not in items[0], "the list must never carry a document body"


# ---------------------------------------------------------------------------------------------
# G-15 — worker never picks up the task: the run stays `queued` and the GET is a 200
# ---------------------------------------------------------------------------------------------


async def test_worker_never_picking_up_a_run_leaves_it_queued_and_get_returns_200(
    client: AsyncClient, app: FastAPI
) -> None:
    run_id = await _create_queued_run(client, app)

    response = await client.get(f"/api/tailoring-runs/{run_id}")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"


# ---------------------------------------------------------------------------------------------
# G-16 … G-23 / AC-12 / AC-13 — every LLM failure class is recorded `failed`, never a 5xx, with the
# correct `retryable` value. Table-driven over all seven exception-backed reasons; `not_queued`
# (G-14, above) and `abandoned` (G-25, below) round out AC-13's "all nine reasons".
# ---------------------------------------------------------------------------------------------

_LLM_FAILURE_CASES: list[tuple[Callable[[], TailoringFailed], str, bool]] = [
    (LlmUnavailable, "llm_unavailable", True),
    (LlmRateLimited, "llm_rate_limited", True),
    (LlmRefused, "llm_refused", False),
    (LlmTimedOut, "llm_timed_out", True),
    (lambda: LlmOutputInvalid("not_json"), "llm_output_invalid", True),
    (LlmInputsTooLarge, "inputs_too_large", False),
    (LlmError, "llm_error", True),
]
_LLM_FAILURE_IDS = [
    "llm_unavailable",
    "llm_rate_limited",
    "llm_refused",
    "llm_timed_out",
    "llm_output_invalid",
    "inputs_too_large",
    "llm_error",
]


@pytest.mark.parametrize(
    ("make_failure", "expected_reason", "expected_retryable"),
    _LLM_FAILURE_CASES,
    ids=_LLM_FAILURE_IDS,
)
async def test_each_llm_failure_class_is_recorded_failed_never_a_5xx_with_correct_retryable(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    make_failure: Callable[[], TailoringFailed],
    expected_reason: str,
    expected_retryable: bool,
) -> None:
    fake_llm = FakeLlm(make_failure())
    _install_worker_llm(monkeypatch, fake_llm)
    run_id = await _create_queued_run(client, app)

    outcome = await _run_worker(session, settings, run_id)
    assert outcome is ExecuteTailoringRunOutcome.FAILED
    assert len(fake_llm.calls) == 1

    response = await client.get(f"/api/tailoring-runs/{run_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["failure_reason"] == expected_reason
    assert body["retryable"] is expected_retryable
    assert body["tailored_cv"] is None
    assert body["cover_letter"] is None


# ---------------------------------------------------------------------------------------------
# G-25 — a run redelivered after the stale window is recorded failed/abandoned, retryable, with no
# LLM call ever made for the redelivery
# ---------------------------------------------------------------------------------------------


async def test_a_run_abandoned_past_the_stale_window_is_recorded_failed_and_retryable(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
        SqlAlchemyTailoringRunRepository,
    )

    clock0 = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    app.dependency_overrides[get_clock] = lambda: clock0
    run_id = await _create_queued_run(client, app)

    # Simulate a worker that picked the run up and then died mid-call: mark it `running` directly
    # through the repository (bypassing the use case, which would also call the LLM).
    repo = SqlAlchemyTailoringRunRepository(session)
    run = await repo.get(TailoringRunId(UUID(run_id)))
    run.mark_started(clock0.now())
    await repo.save(run)
    await session.commit()

    stale_clock = FixedClock(clock0.now())
    stale_clock.advance(settings.tailoring_stale_after_seconds + 1)
    _install_worker_clock(monkeypatch, stale_clock)
    fake_llm = FakeLlm(_a_draft())
    _install_worker_llm(monkeypatch, fake_llm)

    outcome = await _run_worker(session, settings, run_id)

    assert outcome is ExecuteTailoringRunOutcome.ABANDONED
    assert fake_llm.calls == [], "a redelivery past the stale window must never call the model"

    response = await client.get(f"/api/tailoring-runs/{run_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["failure_reason"] == "abandoned"
    assert body["retryable"] is True


# ---------------------------------------------------------------------------------------------
# G-26 — the task is delivered for a run id that no longer exists: returns MISSING, never raises
# ---------------------------------------------------------------------------------------------


async def test_task_for_an_unknown_run_id_returns_missing_without_raising(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_llm = FakeLlm(_a_draft())
    _install_worker_llm(monkeypatch, fake_llm)

    outcome = await _run_worker(session, settings, str(uuid4()))

    assert outcome is ExecuteTailoringRunOutcome.MISSING
    assert fake_llm.calls == []


# ---------------------------------------------------------------------------------------------
# G-27 / AC-10 — the task is delivered for an already-decided run: SKIPPED, no second LLM call
# ---------------------------------------------------------------------------------------------


async def test_task_for_an_already_decided_run_is_skipped_and_makes_no_second_llm_call(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_llm = FakeLlm(_a_draft())
    _install_worker_llm(monkeypatch, fake_llm)
    run_id = await _create_queued_run(client, app)

    first = await _run_worker(session, settings, run_id)
    assert first is ExecuteTailoringRunOutcome.SUCCEEDED
    assert len(fake_llm.calls) == 1

    second = await _run_worker(session, settings, run_id)

    assert second is ExecuteTailoringRunOutcome.SKIPPED
    assert len(fake_llm.calls) == 1, "AC-10: a redelivered task must not call the LLM a second time"


# ---------------------------------------------------------------------------------------------
# G-29 / AC-14 — a run owned by another session, and a nonexistent id, are byte-identical 404s
# ---------------------------------------------------------------------------------------------


async def test_reading_another_sessions_run_and_a_nonexistent_id_return_byte_identical_404(
    client: AsyncClient, app: FastAPI
) -> None:
    run_id = await _create_queued_run(client, app)

    async with _new_client(app) as other_client:
        await _mint_cookie(other_client)
        not_mine = await other_client.get(f"/api/tailoring-runs/{run_id}")
        never_existed = await other_client.get(f"/api/tailoring-runs/{uuid4()}")

    assert not_mine.status_code == never_existed.status_code == 404
    assert _error_code(not_mine) == _error_code(never_existed) == "tailoring_run_not_found"


# ---------------------------------------------------------------------------------------------
# G-30 — a malformed UUID in the detail path is FastAPI's own 422 (requires a valid cookie —
# finding #2)
# ---------------------------------------------------------------------------------------------


async def test_malformed_uuid_in_path_returns_422(client: AsyncClient) -> None:
    await _mint_cookie(client)

    response = await client.get("/api/tailoring-runs/not-a-uuid")

    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------------------------
# The GETs: an empty list is 200, never 404; both GETs answer Cache-Control: no-store
# ---------------------------------------------------------------------------------------------


async def test_get_list_for_a_session_with_no_runs_returns_an_empty_list_not_404(
    client: AsyncClient,
) -> None:
    await _mint_cookie(client)

    response = await client.get("/api/tailoring-runs")

    assert response.status_code == 200, response.text
    assert response.json() == {"items": []}


async def test_both_gets_answer_cache_control_no_store(client: AsyncClient, app: FastAPI) -> None:
    run_id = await _create_queued_run(client, app)

    detail = await client.get(f"/api/tailoring-runs/{run_id}")
    listing = await client.get("/api/tailoring-runs")

    assert detail.status_code == 200, detail.text
    assert listing.status_code == 200, listing.text
    assert detail.headers.get("cache-control") == "no-store"
    assert listing.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------------------------
# AC-21 — the privacy test: a full successful run AND a full failed run, captured together. No
# fragment of the fixture CV, posting or tailored output may appear anywhere in the logs, and a
# client IP must never appear in a log line at all (a fortiori, never paired with a run id).
# ---------------------------------------------------------------------------------------------


async def test_a_full_successful_and_a_full_failed_run_never_log_cv_posting_or_document_text(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-21, across one successful and one failed run, captured together.

    **This test's honest limit**: `_install_worker_llm` replaces `GeminiLlm` with a fake that never
    logs anything, so this test cannot catch a leak from the adapter itself or from a vendor logger one
    frame below it (the `httpx`/`google.genai` shape 1.2's `/verify` found). That is deliberately T34's
    job — the adapter's own tests drive the real `GeminiLlm` against a stub transport, where such a leak
    is actually reachable. What this test *can* and does prove is the half that runs regardless of which
    adapter is behind the port: that `ExecuteTailoringRun`, `LoggingEventPublisher` and the API layer
    around them never put a CV, a posting or a tailored document into a log line.
    """
    success_cv_marker = "QA_PRIVACY_SUCCESS_CV_TOKEN"
    success_posting_marker = "QA_PRIVACY_SUCCESS_POSTING_TOKEN"
    success_tailored_cv_marker = "QA_PRIVACY_SUCCESS_TAILORED_CV_TOKEN"
    success_letter_marker = "QA_PRIVACY_SUCCESS_TAILORED_LETTER_TOKEN"
    fail_cv_marker = "QA_PRIVACY_FAILED_CV_TOKEN"
    fail_posting_marker = "QA_PRIVACY_FAILED_POSTING_TOKEN"
    privacy_ip = "203.0.113.77"  # TEST-NET-3 (RFC 5737) — safe to use in a test, never routable

    with caplog.at_level(logging.INFO):
        # --- a full successful run ---
        fake_llm_ok = FakeLlm(
            _a_draft(cv_marker=success_tailored_cv_marker, letter_marker=success_letter_marker)
        )
        _install_worker_llm(monkeypatch, fake_llm_ok)
        _install_queue(app)
        base_cv_id, job_posting_id = await _ready_inputs(
            client,
            cv_text=_cv_body_text(success_cv_marker),
            posting_text=_posting_body_text(success_posting_marker),
        )
        create_ok = await client.post(
            "/api/tailoring-runs",
            json={"base_cv_id": base_cv_id, "job_posting_id": job_posting_id},
            headers={"x-forwarded-for": privacy_ip},
        )
        assert create_ok.status_code == 202, create_ok.text
        ok_run_id = create_ok.json()["id"]
        ok_outcome = await _run_worker(session, settings, ok_run_id)
        assert ok_outcome is ExecuteTailoringRunOutcome.SUCCEEDED
        assert len(fake_llm_ok.calls) == 1, (
            "the fake must actually have been used, or the successful run never produced the "
            "tailored-document markers this test goes on to assert are absent from the logs"
        )

        # --- a full failed run ---
        fake_llm_fail = FakeLlm(LlmOutputInvalid("not_json"))
        _install_worker_llm(monkeypatch, fake_llm_fail)
        _install_queue(app)
        fail_base_cv_id, fail_job_posting_id = await _ready_inputs(
            client,
            cv_text=_cv_body_text(fail_cv_marker),
            posting_text=_posting_body_text(fail_posting_marker),
        )
        create_fail = await client.post(
            "/api/tailoring-runs",
            json={"base_cv_id": fail_base_cv_id, "job_posting_id": fail_job_posting_id},
            headers={"x-forwarded-for": privacy_ip},
        )
        assert create_fail.status_code == 202, create_fail.text
        fail_run_id = create_fail.json()["id"]
        fail_outcome = await _run_worker(session, settings, fail_run_id)
        assert fail_outcome is ExecuteTailoringRunOutcome.FAILED
        assert len(fake_llm_fail.calls) == 1, (
            "the fake must actually have been used, or the failed run never went through the path "
            "this test means to check"
        )

    log_output = caplog.text

    # The positive proof this test's negative assertions below depend on: `assert caplog.records`
    # alone would pass on any record at all — SQLAlchemy, httpx, uvicorn — and say nothing about
    # whether the *tailoring* channel was ever seen. AC-21 requires `tailoring_run_id` to be logged,
    # so its presence for BOTH runs is exactly the right positive check: it proves the domain-event
    # log line this test's absence-of-markers assertions rely on was actually captured, for both the
    # succeeded and the failed run, not just for whichever request happened to log first.
    assert ok_run_id in log_output, (
        "expected the successful run's tailoring_run_id to appear in the logs (AC-21) — its absence "
        "means the tailoring channel was never captured, and the marker-absence assertions below "
        "would pass vacuously"
    )
    assert fail_run_id in log_output, (
        "expected the failed run's tailoring_run_id to appear in the logs (AC-21) — same guard, for "
        "the failed half of this test"
    )
    assert "TailoringRunSucceeded" in log_output, (
        "expected the successful run's own domain event to have been logged"
    )
    assert "TailoringRunFailed" in log_output, (
        "expected the failed run's own domain event to have been logged"
    )

    for marker in (
        success_cv_marker,
        success_posting_marker,
        success_tailored_cv_marker,
        success_letter_marker,
        fail_cv_marker,
        fail_posting_marker,
    ):
        assert marker not in log_output, f"{marker!r} leaked into the logs"

    assert privacy_ip not in log_output, (
        "the client IP must never reach a log line (Constitution §8) — a fortiori never paired with "
        "a tailoring_run_id"
    )
