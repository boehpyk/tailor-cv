"""API tests for the inline export pair: `GET /api/tailoring-runs/{id}/documents/{kind}/download`.

**This is the RED half of a red-first cycle** (CLAUDE.md, docs/sdlc.md §2, I17). Every test here is
written against `docs/specs/export-multi-format-download/feature-spec.md`'s failure contract
(X-1…X-9) and acceptance criteria AC-8…AC-11, AC-27 — not against
`infrastructure/api/routers/export.py::download_document_inline`, whose body is currently a single
`raise NotImplementedError` (I16's SKELETON). The router IS mounted (`create_app` includes it), so a
passing collection with every behavioural test failing on a real assertion — never a bare
`ImportError`, never a 404 from a missing route, never a 422 from a broken signature — is what a
correct RED run looks like here.

**`require_guest_session` runs before FastAPI's own parameter validation** (I16's own finding,
measured): a cookie-less request with a malformed run id, an unknown `kind` or a missing/unknown
`format` all answer 401, not 422. Every test below that means to assert a 422 therefore mints a real
guest-session cookie first (`_mint_cookie` / `_ready_inputs`), exactly as `test_tailoring.py` and
`test_tailoring_revise.py` both already establish for their own routers.

**Runs are built directly through the aggregate and the repository, not through
`POST /api/tailoring-runs` plus a worker.** `TailoringRun.request` / `mark_started` /
`mark_succeeded` are production code, called rather than reimplemented, bound to this test's own
`session` — the identical technique `test_tailoring_revise.py`'s module docstring explains at
length, reused here because a synchronous, in-process succeeded run is what every one of these tests
needs and a real worker round-trip buys nothing extra for this router.

**Why this file's `client` fixture is not the shared one.** `ASGITransport`'s default
`raise_app_exceptions=True` turns a skeleton's `NotImplementedError` into a Python exception that
aborts the test rather than a real 500 response — the assertion this file exists to make would never
run. `clear_redis` rides every test as an autouse fixture: nothing here posts against
`export:create`, but the rate limiter dependency is still resolved on `GET`... in fact it is not (the
inline route carries no rate limiter at all), so this file does not strictly need it — it is applied
anyway for the same reason `test_tailoring.py` applies it everywhere: consistency with every other
API test module, and because a future edit that adds a limiter to this route should not have to
remember to also add the fixture.

**What a "legitimate red" looks like in this file.** With the router's body still `raise
NotImplementedError`, `main.py` has no handler registered for a bare `NotImplementedError` — it
propagates through `ServerErrorMiddleware`, which `raise_app_exceptions=False` turns into a plain
500 response with no JSON envelope. Every test below that reaches the handler body therefore fails
on `assert response.status_code == <real code> ` seeing `500` instead — a real assertion failure,
never a 404 (the route exists) and never a 422 from a broken signature (FastAPI's own validation,
where it applies, already returns the right code today because it runs *before* the handler body).
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text as sql_text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.export.errors import (
    DocumentRenderError,
    DocumentRenderFailed,
    DocumentRenderTimedOut,
)
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocuments,
    TailoringFailureReason,
    TailoringRunId,
)
from tailorcraft.infrastructure.api.deps import get_app_settings, get_document_renderer
from tailorcraft.infrastructure.api.guest_session import COOKIE_NAME
from tailorcraft.infrastructure.settings import Settings
from tests.integration.fakes import FakeDocumentRenderer

# ---------------------------------------------------------------------------------------------
# Fixture text — comfortably past every value object's floor, distinctive enough for the privacy
# instincts of this suite even though this file's own privacy assertions live in test_export.py.
# ---------------------------------------------------------------------------------------------


def _cv_body_text(marker: str = "QA_EXPORT_INLINE_CV_TOKEN") -> str:
    filler = "Senior backend engineer with a decade leading platform reliability work. " * 8
    return f"{marker} {filler}"


def _posting_body_text(marker: str = "QA_EXPORT_INLINE_POSTING_TOKEN") -> str:
    filler = "We are hiring a senior engineer to own reliability and mentor the team. " * 4
    return f"{marker} {filler}"


def _tailored_cv_text(marker: str = "QA_EXPORT_DRAFT_CV_TOKEN", *, body: str | None = None) -> str:
    if body is not None:
        return body
    filler = "Rewrote the platform reliability program end to end for this role. " * 8
    return f"{marker} {filler}".rstrip()


def _cover_letter_text(
    marker: str = "QA_EXPORT_DRAFT_LETTER_TOKEN", *, body: str | None = None
) -> str:
    if body is not None:
        return body
    filler = "I am applying because this role matches my reliability background. " * 6
    return f"{marker} {filler}".rstrip()


def _now_whole_second() -> datetime:
    """`datetime.now()`, truncated to the whole-second contract (ADR-0007) — used only where this
    file builds a run directly through the aggregate rather than through the API's own `Clock`
    port, so it can never precede a real wall-clock instant an earlier step used."""
    return datetime.now(UTC).replace(microsecond=0)


# ---------------------------------------------------------------------------------------------
# Small HTTP helpers — mirror test_tailoring.py's and test_tailoring_revise.py's
# ---------------------------------------------------------------------------------------------


def _error_code(response: Response) -> str:
    # A skeleton's bare `NotImplementedError` answers with Starlette's default plain-text 500 page,
    # not a JSON envelope — `response.json()` on that body raises `JSONDecodeError`, which pytest
    # reports as an unhandled exception rather than a real assertion failure (a weaker red than the
    # one this test means to record). Converted to `AssertionError` here so every caller's red is on
    # an assertion, never on a decode crash — the eventual check (`"error" in body`) is unchanged.
    try:
        body = response.json()
    except ValueError as exc:
        raise AssertionError(
            f"expected a JSON {{'error': {{...}}}} envelope, got a non-JSON "
            f"{response.status_code} body: {response.text[:200]!r}"
        ) from exc
    assert "error" in body, f"expected the {{'error': {{...}}}} envelope, got {body!r}"
    return str(body["error"]["code"])


def _guest_cookie_header(response: Response) -> str | None:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{COOKIE_NAME}="):
            return header
    return None


def _new_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
    )


def _install_renderer(
    app: FastAPI, outcome: bytes | DocumentRenderFailed, *, delay_seconds: float = 0.0
) -> FakeDocumentRenderer:
    """Override the inline render port with a fake that either returns fixed bytes or raises a
    fixed `DocumentRenderFailed` subclass."""
    renderer = FakeDocumentRenderer(outcome, delay_seconds=delay_seconds)
    app.dependency_overrides[get_document_renderer] = lambda: renderer
    return renderer


# ---------------------------------------------------------------------------------------------
# Seeding — CV/posting through the real, already-shipped `intake`/`posting` HTTP surfaces; the run
# itself built directly through the aggregate and the repository (test_tailoring_revise.py's
# technique, reused for the identical reason: this router's own handler is the thing under test,
# and a worker round-trip would only add noise between the setup and the assertion).
# ---------------------------------------------------------------------------------------------


async def _mint_cookie(client: AsyncClient) -> None:
    response = await client.post(
        "/api/base-cvs", files={"file": ("mint.txt", _cv_body_text("MINT").encode(), "text/plain")}
    )
    assert response.status_code == 201, response.text


async def _upload_ready_base_cv(client: AsyncClient, *, text: str | None = None) -> str:
    body_text = text if text is not None else _cv_body_text()
    response = await client.post(
        "/api/base-cvs", files={"file": ("cv.txt", body_text.encode(), "text/plain")}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "extracted", body
    return str(body["id"])


async def _create_ready_job_posting(client: AsyncClient, *, text: str | None = None) -> str:
    body_text = text if text is not None else _posting_body_text()
    response = await client.post("/api/job-postings", json={"source": "pasted", "text": body_text})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _ready_inputs(
    client: AsyncClient, *, cv_text: str | None = None, posting_text: str | None = None
) -> tuple[str, str]:
    base_cv_id = await _upload_ready_base_cv(client, text=cv_text)
    job_posting_id = await _create_ready_job_posting(client, text=posting_text)
    return base_cv_id, job_posting_id


async def _guest_session_id_for(session: AsyncSession, base_cv_id: str) -> UUID:
    result = await session.execute(
        sql_text("SELECT guest_session_id FROM intake_base_cv WHERE id = :id"),
        {"id": UUID(base_cv_id)},
    )
    return UUID(str(result.scalar_one()))


def _repo(session: AsyncSession) -> object:
    # Deferred import: reads mapped attributes at import time, which only exist once
    # `configure_mappings()` has run (conftest.py's session-scoped `_mappings` fixture) — true by
    # the time any test body executes, not yet true at collection time if this were module-level.
    from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
        SqlAlchemyTailoringRunRepository,
    )

    return SqlAlchemyTailoringRunRepository(session)


async def _create_queued_run(
    client: AsyncClient,
    session: AsyncSession,
    *,
    cv_text: str | None = None,
    posting_text: str | None = None,
) -> str:
    from tailorcraft.domain.tailoring.tailoring_run import TailoringRun

    base_cv_id, job_posting_id = await _ready_inputs(
        client, cv_text=cv_text, posting_text=posting_text
    )
    guest_session_id = await _guest_session_id_for(session, base_cv_id)

    repo = _repo(session)
    run = TailoringRun.request(
        id=repo.next_identity(),  # type: ignore[attr-defined]
        guest_session_id=GuestSessionId(guest_session_id),
        base_cv_id=BaseCvId(UUID(base_cv_id)),
        job_posting_id=JobPostingId(UUID(job_posting_id)),
        requested_at=_now_whole_second(),
    )
    await repo.add(run)  # type: ignore[attr-defined]
    await session.commit()
    return str(run.id.value)


async def _advance_to_running(client: AsyncClient, session: AsyncSession) -> str:
    run_id = await _create_queued_run(client, session)
    repo = _repo(session)
    run = await repo.get(TailoringRunId(UUID(run_id)))  # type: ignore[attr-defined]
    run.mark_started(_now_whole_second())
    await repo.save(run)  # type: ignore[attr-defined]
    await session.commit()
    return run_id


async def _create_failed_run(client: AsyncClient, session: AsyncSession) -> str:
    run_id = await _advance_to_running(client, session)
    repo = _repo(session)
    run = await repo.get(TailoringRunId(UUID(run_id)))  # type: ignore[attr-defined]
    run.mark_failed(TailoringFailureReason.LLM_REFUSED, _now_whole_second())
    await repo.save(run)  # type: ignore[attr-defined]
    await session.commit()
    return run_id


async def _create_succeeded_run(
    client: AsyncClient,
    session: AsyncSession,
    *,
    cv_body: str | None = None,
    letter_body: str | None = None,
    cv_marker: str = "QA_EXPORT_DRAFT_CV_TOKEN",
    letter_marker: str = "QA_EXPORT_DRAFT_LETTER_TOKEN",
) -> str:
    run_id = await _advance_to_running(client, session)
    repo = _repo(session)
    run = await repo.get(TailoringRunId(UUID(run_id)))  # type: ignore[attr-defined]
    run.mark_succeeded(
        TailoredDocuments(
            cv=TailoredCv(_tailored_cv_text(cv_marker, body=cv_body)),
            cover_letter=CoverLetter(_cover_letter_text(letter_marker, body=letter_body)),
        ),
        LlmCallMetrics(
            model=ModelName("gemini-test"),
            prompt_version=PromptVersion("1"),
            prompt_tokens=111,
            completion_tokens=222,
            duration_ms=1234,
        ),
        _now_whole_second(),
    )
    await repo.save(run)  # type: ignore[attr-defined]
    await session.commit()
    return run_id


async def _row_version(session: AsyncSession, run_id: str) -> int:
    """The row's live `version` (TR-8), read back rather than hard-coded — `test_tailoring_revise.py`
    establishes the same helper for the same reason. `_create_succeeded_run`'s request -> start ->
    succeed sequence already bumps `version` to 3 before any of these tests revise anything, so a
    caller that hard-codes `expected_version=1` gets `TailoredDocumentVersionConflict` (409) out of
    its own setup, not out of the router under test."""
    result = await session.execute(
        sql_text("SELECT version FROM tailoring_run WHERE id = :id"), {"id": UUID(run_id)}
    )
    return int(result.scalar_one())


async def _revise_cv(
    client: AsyncClient, run_id: str, *, content: str, expected_version: int
) -> None:
    response = await client.put(
        f"/api/tailoring-runs/{run_id}/documents/cv",
        json={"content": content, "expected_version": expected_version},
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------------------------
# Module-local fixtures
# ---------------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Shadows conftest.py's `client` — `raise_app_exceptions=False`, for the reason the module
    docstring gives: a skeleton's `NotImplementedError` must come back as a real 500 response, not
    abort the test."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    return None


# ---------------------------------------------------------------------------------------------
# X-1 / AC-10 — malformed run id, unknown kind, missing/unknown format, or a queued format on the
# inline endpoint: 422 validation_error. Every case needs a real cookie (I16's finding #1).
# ---------------------------------------------------------------------------------------------


async def test_malformed_run_id_returns_422_validation_error(client: AsyncClient) -> None:
    await _mint_cookie(client)

    response = await client.get("/api/tailoring-runs/not-a-uuid/documents/cv/download?format=md")

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


async def test_unknown_document_kind_returns_422_validation_error(client: AsyncClient) -> None:
    await _mint_cookie(client)

    response = await client.get(
        f"/api/tailoring-runs/{uuid4()}/documents/resume/download?format=md"
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


async def test_missing_format_returns_422_validation_error(client: AsyncClient) -> None:
    await _mint_cookie(client)

    response = await client.get(f"/api/tailoring-runs/{uuid4()}/documents/cv/download")

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


async def test_unknown_format_returns_422_validation_error(client: AsyncClient) -> None:
    await _mint_cookie(client)

    response = await client.get(f"/api/tailoring-runs/{uuid4()}/documents/cv/download?format=rtf")

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


@pytest.mark.parametrize("queued_format", ["pdf", "docx"])
async def test_queued_format_on_inline_endpoint_returns_422_validation_error(
    client: AsyncClient, queued_format: str
) -> None:
    """AC-10: the query parameter's type is the inline subset (`Literal["md", "txt"]`), so a
    queued format is unrepresentable here rather than rejected by a handler. **Struck 2026-09-17:
    the message no longer names the exports endpoint** — X-1's "User sees" column reads "— (the
    honest client never sends one)", so this asserts only the `code`, never a sentence."""
    await _mint_cookie(client)

    response = await client.get(
        f"/api/tailoring-runs/{uuid4()}/documents/cv/download?format={queued_format}"
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


# ---------------------------------------------------------------------------------------------
# X-2 — no / unknown / expired guest cookie: 401, no session minted
# ---------------------------------------------------------------------------------------------


async def test_cookieless_get_returns_401_and_mints_no_session(
    client: AsyncClient, session: AsyncSession
) -> None:
    before = await session.execute(sql_text("SELECT count(*) FROM identity_guest_session"))
    before_count = before.scalar_one()

    response = await client.get(f"/api/tailoring-runs/{uuid4()}/documents/cv/download?format=md")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"
    assert _guest_cookie_header(response) is None, (
        "this GET must never mint a session (ADR-0014 §3)"
    )
    after = await session.execute(sql_text("SELECT count(*) FROM identity_guest_session"))
    assert after.scalar_one() == before_count


async def test_unknown_cookie_returns_401(client: AsyncClient) -> None:
    client.cookies.set(COOKIE_NAME, "not-a-real-token")

    response = await client.get(f"/api/tailoring-runs/{uuid4()}/documents/cv/download?format=md")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


# ---------------------------------------------------------------------------------------------
# X-3 — run does not exist, or belongs to another session: identical 404
# ---------------------------------------------------------------------------------------------


async def test_nonexistent_run_returns_404(client: AsyncClient) -> None:
    await _mint_cookie(client)

    response = await client.get(f"/api/tailoring-runs/{uuid4()}/documents/cv/download?format=md")

    assert response.status_code == 404, response.text
    assert _error_code(response) == "tailoring_run_not_found"


async def test_run_owned_by_another_session_returns_the_same_404(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    async with _new_client(app) as other_client:
        foreign_run_id = await _create_succeeded_run(other_client, session)

    await _mint_cookie(client)
    response = await client.get(
        f"/api/tailoring-runs/{foreign_run_id}/documents/cv/download?format=md"
    )

    assert response.status_code == 404, response.text
    assert _error_code(response) == "tailoring_run_not_found"


# ---------------------------------------------------------------------------------------------
# X-4 — the run is queued, running or failed: 409 tailoring_run_not_exportable, body carries status
# ---------------------------------------------------------------------------------------------


async def test_queued_run_returns_409(client: AsyncClient, session: AsyncSession) -> None:
    run_id = await _create_queued_run(client, session)

    response = await client.get(f"/api/tailoring-runs/{run_id}/documents/cv/download?format=md")

    assert response.status_code == 409, response.text
    assert _error_code(response) == "tailoring_run_not_exportable"
    assert response.json()["error"]["status"] == "queued"


async def test_running_run_returns_409(client: AsyncClient, session: AsyncSession) -> None:
    run_id = await _advance_to_running(client, session)

    response = await client.get(f"/api/tailoring-runs/{run_id}/documents/cv/download?format=md")

    assert response.status_code == 409, response.text
    assert _error_code(response) == "tailoring_run_not_exportable"
    assert response.json()["error"]["status"] == "running"


async def test_failed_run_returns_409(client: AsyncClient, session: AsyncSession) -> None:
    run_id = await _create_failed_run(client, session)

    response = await client.get(f"/api/tailoring-runs/{run_id}/documents/cv/download?format=md")

    assert response.status_code == 409, response.text
    assert _error_code(response) == "tailoring_run_not_exportable"
    assert response.json()["error"]["status"] == "failed"


# ---------------------------------------------------------------------------------------------
# X-5 — the inline renderer raises on the document: 500 render_failed, the one deliberate 500
# ---------------------------------------------------------------------------------------------


async def test_renderer_failure_returns_500_render_failed(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    _install_renderer(app, DocumentRenderError())
    run_id = await _create_succeeded_run(client, session)

    response = await client.get(f"/api/tailoring-runs/{run_id}/documents/cv/download?format=txt")

    assert response.status_code == 500, response.text
    assert _error_code(response) == "render_failed"


# ---------------------------------------------------------------------------------------------
# X-6 — the render exceeds export_inline_timeout_seconds: 503 render_timed_out
# ---------------------------------------------------------------------------------------------


async def test_renderer_timeout_returns_503_render_timed_out(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    _install_renderer(app, DocumentRenderTimedOut(), delay_seconds=0.05)
    run_id = await _create_succeeded_run(client, session)

    response = await client.get(f"/api/tailoring-runs/{run_id}/documents/cv/download?format=txt")

    assert response.status_code == 503, response.text
    assert _error_code(response) == "render_timed_out"


# ---------------------------------------------------------------------------------------------
# X-7 — Postgres down on the read: 503 service_unavailable (the app-level SQLAlchemyError handler)
# ---------------------------------------------------------------------------------------------


async def test_database_failure_on_the_read_returns_503_service_unavailable(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await _create_succeeded_run(client, session)

    async def _raise_sqlalchemy_error(*args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("simulated read failure (X-7)")

    monkeypatch.setattr(session, "execute", _raise_sqlalchemy_error)

    response = await client.get(f"/api/tailoring-runs/{run_id}/documents/cv/download?format=md")

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"


# ---------------------------------------------------------------------------------------------
# X-8 — the current document is the draft (never edited) vs a revision, for both kinds
# ---------------------------------------------------------------------------------------------


async def test_unedited_cv_download_serves_the_draft(
    client: AsyncClient, session: AsyncSession
) -> None:
    run_id = await _create_succeeded_run(client, session, cv_marker="QA_X8_DRAFT_CV")

    response = await client.get(f"/api/tailoring-runs/{run_id}/documents/cv/download?format=md")

    assert response.status_code == 200, response.text
    assert "QA_X8_DRAFT_CV" in response.text


async def test_edited_cv_download_serves_the_revision_not_the_draft(
    client: AsyncClient, session: AsyncSession
) -> None:
    run_id = await _create_succeeded_run(client, session, cv_marker="QA_X8_DRAFT_CV")
    revised_text = _tailored_cv_text("QA_X8_REVISED_CV")
    await _revise_cv(
        client, run_id, content=revised_text, expected_version=await _row_version(session, run_id)
    )

    response = await client.get(f"/api/tailoring-runs/{run_id}/documents/cv/download?format=md")

    assert response.status_code == 200, response.text
    assert "QA_X8_REVISED_CV" in response.text
    assert "QA_X8_DRAFT_CV" not in response.text, (
        "once a revision exists it is current — the draft must not still be served (ADR-0015 §1)"
    )


async def test_unedited_cover_letter_download_serves_the_draft(
    client: AsyncClient, session: AsyncSession
) -> None:
    run_id = await _create_succeeded_run(client, session, letter_marker="QA_X8_DRAFT_LETTER")

    response = await client.get(
        f"/api/tailoring-runs/{run_id}/documents/cover_letter/download?format=md"
    )

    assert response.status_code == 200, response.text
    assert "QA_X8_DRAFT_LETTER" in response.text


# ---------------------------------------------------------------------------------------------
# X-9 — hostile content: raw HTML, a <script>, a javascript: link, a table, a fenced block
# ---------------------------------------------------------------------------------------------

_HOSTILE_CV_BODY = (
    "# Jane Doe\n\n"
    "<script>alert(1)</script>\n\n"
    "[click me](javascript:alert(1))\n\n"
    "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
    "```\ncode fence contents\n```\n\n"
    "Rewrote the platform reliability program end to end for this role, twice over, at scale. " * 6
)


async def test_md_download_serves_the_stored_markdown_verbatim_including_hostile_markup(
    client: AsyncClient, session: AsyncSession
) -> None:
    """X-9's md branch: served as-is, `text/markdown` **attachment**, never rendered — the
    `<script>` and the `javascript:` link survive byte for byte because this is the user's own
    text, offered for download rather than displayed."""
    run_id = await _create_succeeded_run(client, session, cv_body=_HOSTILE_CV_BODY)

    response = await client.get(f"/api/tailoring-runs/{run_id}/documents/cv/download?format=md")

    assert response.status_code == 200, response.text
    assert "<script>alert(1)</script>" in response.text
    assert "[click me](javascript:alert(1))" in response.text


async def test_txt_download_renders_hostile_markup_as_literal_text_with_no_script_execution_path(
    client: AsyncClient, session: AsyncSession
) -> None:
    """X-9's txt branch: `html=False` makes the `<script>` a text token, so it survives as literal
    text (there is no HTML markup in the stream to strip); the javascript: link's text survives
    with no URL, because `validateLink` accepts only http/https/mailto; the table and the fenced
    block are emitted as text, not as a table or a `<pre>`."""
    run_id = await _create_succeeded_run(client, session, cv_body=_HOSTILE_CV_BODY)

    response = await client.get(f"/api/tailoring-runs/{run_id}/documents/cv/download?format=txt")

    assert response.status_code == 200, response.text
    body = response.text
    assert "<script>alert(1)</script>" in body, "raw HTML must survive as literal text (html=False)"
    assert "click me" in body
    assert "javascript:" not in body, "a disallowed scheme's URL must never appear in the output"
    assert "code fence contents" in body


# ---------------------------------------------------------------------------------------------
# AC-27 — Content-Disposition filenames are constants; a Content-Disposition injection attempt in
# the document's first line must not reach the header
# ---------------------------------------------------------------------------------------------


async def test_content_disposition_injection_attempt_is_ignored_header_is_the_constant(
    client: AsyncClient, session: AsyncSession
) -> None:
    hostile_first_line = '"; filename="evil.exe'
    body = f"{hostile_first_line}\n\n" + _tailored_cv_text("QA_AC27")
    run_id = await _create_succeeded_run(client, session, cv_body=body)

    response = await client.get(f"/api/tailoring-runs/{run_id}/documents/cv/download?format=md")

    assert response.status_code == 200, response.text
    assert response.headers.get("content-disposition") == 'attachment; filename="tailored-cv.md"', (
        "the header must be byte-identical to the domain's constant, never derived from the text"
    )


# ---------------------------------------------------------------------------------------------
# AC-8 — headers, byte for byte, for both kinds and both inline formats
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "format", "content_type", "filename"),
    [
        pytest.param("cv", "md", "text/markdown; charset=utf-8", "tailored-cv.md", id="cv-md"),
        pytest.param("cv", "txt", "text/plain; charset=utf-8", "tailored-cv.txt", id="cv-txt"),
        pytest.param(
            "cover_letter",
            "md",
            "text/markdown; charset=utf-8",
            "cover-letter.md",
            id="letter-md",
        ),
        pytest.param(
            "cover_letter",
            "txt",
            "text/plain; charset=utf-8",
            "cover-letter.txt",
            id="letter-txt",
        ),
    ],
)
async def test_headers_are_byte_for_byte_the_spec(
    client: AsyncClient,
    session: AsyncSession,
    kind: str,
    format: str,
    content_type: str,
    filename: str,
) -> None:
    run_id = await _create_succeeded_run(client, session)

    response = await client.get(
        f"/api/tailoring-runs/{run_id}/documents/{kind}/download?format={format}"
    )

    assert response.status_code == 200, response.text
    assert response.headers.get("content-type") == content_type
    assert response.headers.get("content-disposition") == f'attachment; filename="{filename}"'
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("content-length") == str(len(response.content))


async def test_no_row_no_file_no_event_no_redis_key_is_written(
    client: AsyncClient, session: AsyncSession
) -> None:
    """AC-8: an inline download is a pure read. There is no `export_job` table row this endpoint
    could write to — the assertion is that the count of jobs of any kind stays zero and the run's
    own version is untouched by a download."""
    run_id = await _create_succeeded_run(client, session)

    before = await session.execute(sql_text("SELECT count(*) FROM export_job"))
    await client.get(f"/api/tailoring-runs/{run_id}/documents/cv/download?format=md")
    after = await session.execute(sql_text("SELECT count(*) FROM export_job"))

    assert after.scalar_one() == before.scalar_one() == 0


# ---------------------------------------------------------------------------------------------
# AC-9 — the txt rendering grammar, table-driven
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("markdown_fragment", "expected_substring", "forbidden_substring"),
    [
        pytest.param("# Big Heading", "Big Heading", "# Big Heading", id="heading-loses-hash"),
        pytest.param("- a bullet", "- a bullet", None, id="bullet-stays-dash"),
        pytest.param("1. first\n2. second", "1. first", None, id="ordered-list-keeps-numbers"),
        pytest.param("**bold text**", "bold text", "**bold text**", id="bold-loses-markers"),
        pytest.param("*italic text*", "italic text", "*italic text*", id="italic-loses-markers"),
        pytest.param(
            "[home](https://example.com)",
            "home (https://example.com)",
            None,
            id="link-renders-as-text-open-paren-url",
        ),
        pytest.param(
            "[bad](ftp://example.com)", "bad", "ftp://example.com", id="disallowed-scheme-drops-url"
        ),
    ],
)
async def test_txt_rendering_grammar_table(
    client: AsyncClient,
    session: AsyncSession,
    markdown_fragment: str,
    expected_substring: str,
    forbidden_substring: str | None,
) -> None:
    # `* 6` left this fixture at 336-363 non-whitespace characters depending on the fragment — under
    # `TailoredCv`'s 400-character floor (OQ-5), so every case in this table raised
    # `TailoredDocumentTooShort` from the fixture before the router was ever reached (a broken test,
    # not a red). `* 8` clears the floor with margin while the discriminating substring under test
    # stays exactly what it was.
    filler = "Rewrote the platform reliability program end to end for this role. " * 8
    body = f"{markdown_fragment}\n\n{filler}"
    run_id = await _create_succeeded_run(client, session, cv_body=body)

    response = await client.get(f"/api/tailoring-runs/{run_id}/documents/cv/download?format=txt")

    assert response.status_code == 200, response.text
    assert expected_substring in response.text
    if forbidden_substring is not None:
        assert forbidden_substring not in response.text


async def test_txt_rendering_separates_blocks_by_one_blank_line(
    client: AsyncClient, session: AsyncSession
) -> None:
    filler = "Rewrote the platform reliability program end to end for this role. " * 6
    body = f"# Heading One\n\n{filler}\n\n# Heading Two\n\n{filler}"
    run_id = await _create_succeeded_run(client, session, cv_body=body)

    response = await client.get(f"/api/tailoring-runs/{run_id}/documents/cv/download?format=txt")

    assert response.status_code == 200, response.text
    assert "Heading One\n\nRewrote" in response.text or "Heading One\n\n" in response.text


# ---------------------------------------------------------------------------------------------
# AC-11 — the inline render runs off the event loop: /health/live must stay responsive while
# concurrent txt downloads of a document at the size ceiling run. `slow`-marked (ADR-0009's method,
# the identical shape test_posting_fetcher_event_loop.py already uses one context over).
# ---------------------------------------------------------------------------------------------


def _document_at_the_ceiling() -> str:
    """A CV comfortably inside `TailoredCv`'s 20,000-character ceiling, built from real words so
    markdown-it has actual parsing to do rather than one long token."""
    sentence = "Rewrote the platform reliability program end to end for this role. "
    text = sentence * 280  # ~19,600 characters, safely under the 20,000 ceiling
    return text[:19_900]


def _letter_at_the_ceiling() -> str:
    """A cover letter comfortably inside `CoverLetter`'s 8,000-character ceiling — **not** the same
    text as `_document_at_the_ceiling()`. The original fixture passed that ~19,900-character CV
    body as `letter_body=` too, and `CoverLetter`'s ceiling is 8,000, not 20,000 — the run never got
    built (`TailoredDocumentTooLong`) and the test never reached the router at all (a broken
    fixture, not a red). This is sized the same way, against `CoverLetter`'s own ceiling."""
    sentence = "Rewrote the platform reliability program end to end for this role. "
    text = sentence * 120  # ~8,400 characters
    return text[:7_900]


async def _hammer_health_live(client: AsyncClient, *, stop: asyncio.Event) -> list[float]:
    """See `test_posting_fetcher_event_loop.py::_hammer_health_live` for the full account of why
    the trailing sleep is load-bearing against an in-process ASGI transport with no real I/O."""
    latencies: list[float] = []
    while not stop.is_set():
        started = time.perf_counter()
        response = await client.get("/health/live")
        latencies.append(time.perf_counter() - started)
        assert response.status_code == 200
        await asyncio.sleep(0.001)
    return latencies


@pytest.mark.slow
async def test_health_live_stays_responsive_during_twenty_concurrent_txt_downloads(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    # Twenty runs, each seeded through the real `/api/base-cvs` and `/api/job-postings` surfaces
    # under ONE guest session (the point of the test is one session hammering `/health/live`, not
    # twenty separate ones). Four independent caps and rate limits — 5 base CVs, 10 job postings per
    # session, and 10/20 uploads/postings per hour — exist to bound a single visitor's footprint and
    # are unrelated to what AC-11 measures, so they are raised for this test only rather than routed
    # around. `TailoringRun` itself is built directly through the aggregate and the repository below
    # (`_create_queued_run`'s established technique), never through `POST /api/tailoring-runs`, so
    # `max_tailoring_runs_per_session` never applies here and needs no override.
    modified_settings = settings.model_copy(
        update={
            "max_base_cvs_per_session": 25,
            "max_job_postings_per_session": 25,
            "upload_rate_limit_per_hour": 25,
            "upload_rate_limit_per_ip_per_hour": 25,
            "posting_rate_limit_per_hour": 25,
        }
    )
    app.dependency_overrides[get_app_settings] = lambda: modified_settings

    document = _document_at_the_ceiling()
    letter = _letter_at_the_ceiling()
    run_ids = [
        await _create_succeeded_run(client, session, cv_body=document, letter_body=letter)
        for _ in range(20)
    ]

    stop = asyncio.Event()
    hammer_task = asyncio.ensure_future(_hammer_health_live(client, stop=stop))
    await asyncio.sleep(0)

    try:
        responses = await asyncio.gather(
            *(
                client.get(f"/api/tailoring-runs/{run_id}/documents/cv/download?format=txt")
                for run_id in run_ids
            )
        )
    finally:
        stop.set()
    latencies = await asyncio.wait_for(hammer_task, timeout=10)

    for response in responses:
        assert response.status_code == 200, response.text

    assert len(latencies) >= 20, (
        f"only {len(latencies)} /health/live samples were taken during the 20 concurrent "
        "downloads — too few to say anything about event-loop liveness"
    )
    p50 = statistics.median(latencies)
    assert p50 < 0.005, (
        f"/health/live p50 was {p50 * 1000:.2f} ms during 20 concurrent inline downloads "
        f"(n={len(latencies)}, max={max(latencies) * 1000:.2f} ms) — the event loop was blocked"
    )
    assert max(latencies) < 0.050, (
        f"/health/live max latency was {max(latencies) * 1000:.2f} ms — a single stall over 50 ms "
        "during 20 concurrent inline downloads (AC-11)"
    )
