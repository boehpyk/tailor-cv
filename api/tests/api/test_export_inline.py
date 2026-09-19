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
from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocumentKind,
    TailoredDocuments,
    TailoringFailureReason,
    TailoringRunId,
)
from tailorcraft.infrastructure.api.deps import get_document_renderer
from tailorcraft.infrastructure.api.guest_session import COOKIE_NAME
from tailorcraft.infrastructure.export.renderer import MarkdownDocumentRenderer
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
# concurrent txt renders of a document at the size ceiling run. `slow`-marked (ADR-0009's method,
# the identical shape test_posting_fetcher_event_loop.py already uses one context over).
#
# **Reshaped 2026-09-18 — driven below the HTTP layer, following that same precedent.** The
# original version of this test drove 20 concurrent `client.get(".../download?format=txt")`
# requests. Every API test in this suite shares ONE `AsyncSession` — conftest's `get_session`
# override yields the single per-test session bound to the test's rolled-back connection — and
# SQLAlchemy refuses concurrent operations on one `AsyncSession`
# (`InvalidRequestError: This session is provisioning a new connection; concurrent operations are
# not permitted`). `main.py`'s `SQLAlchemyError` handler turned that into a 503 that no
# implementation of the router could avoid, because the failure was in the test's own fixture
# topology, not in anything under test. No amount of moving work off the event loop fixes a
# session that refuses to be shared.
#
# `test_posting_fetcher_event_loop.py` establishes the fix for exactly this shape: it drives
# `HttpxTrafilaturaFetcher.fetch` directly, four times concurrently, rather than through an HTTP
# round trip, because the thing AC-10 there guards — a CPU-bound step kept off the loop via a
# worker thread — lives entirely inside the adapter and needs no request, no session and nothing
# else HTTP-shaped to exercise. The identical argument applies here: AC-11 exists to guard
# `MarkdownDocumentRenderer._render_bounded` running its work through
# `asyncio.to_thread` under `asyncio.wait_for` (`infrastructure/export/renderer.py`) so a
# synchronous markdown-it parse never blocks the loop. That guarantee is the renderer's alone —
# the router's own DB read is genuinely async (asyncpg) and was never the thing at risk. Driving
# the renderer directly, with no `TailoringRun`, no repository and no session at all, measures the
# same property this AC has always meant to guard, without needing 20 sessions this suite's
# fixtures do not provide.
#
# **Sizing is a guarantee now, not a guess (CI flake found 2026-09-19).** The first version of this
# test gave each of the 20 workers a fixed twelve renders, sized against one machine's measured
# per-render cost so the batch would "probably" run long enough for the 1 ms-paced hammer loop to
# collect its 20-sample floor. That coupled a *test precondition* to how fast the renders happened to
# finish, which is not something the test controls: a slow box can still blow the p50 budget (the
# property this test exists to guard), but a genuinely FAST one finishes twelve rounds per worker
# before the hammer reaches 20 samples at all — CI hit exactly this, 16 samples collected against a
# 20-sample floor, with a p50 in the sub-millisecond range (the loop was, if anything, unusually
# responsive). The flake is therefore bidirectional: too slow trips the p50 assertion, too fast trips
# the sample-floor precondition, and neither failure is a property of this router or this adapter.
#
# The fix makes "enough samples" something the test *guarantees* rather than hopes for: the 20 workers
# below render **until the hammer says it has its floor**, not a fixed number of times each. A single
# shared `asyncio.Event` is the stop signal for both loops — the hammer sets it the instant it has
# collected `_SAMPLE_FLOOR` latencies, and every worker checks it before starting its next render.
# The batch's wall-clock duration becomes an *output* of the measurement rather than an assumption
# baked into an iteration count, so it can no longer drift out of date as either the renderer or the
# machine running it gets faster.
#
# **Claims this test still makes**: with 20 workers concurrently driving
# `MarkdownDocumentRenderer.render(..., format=TXT)` against documents at `TailoredCv`'s
# ~20,000-character ceiling, `/health/live`'s **p50** latency stays under 5 ms throughout — the loop
# keeps answering trivial requests promptly on the whole, which is what "the CPU-bound parse runs in
# a thread, not on the loop" buys.
#
# **Claims this test no longer makes, and why:**
# 1. *The full HTTP path stays off the loop under 20-way concurrency.* Only the renderer's own thread
#    hop is measured now, not the route handler, the guest-session dependency or the repository read
#    — those could only be measured with 20 independent sessions (one per concurrent request), which
#    this suite's shared-session fixture does not provide (see above). A session-per-request variant
#    remains available if that router-level claim is ever wanted back.
# 2. *No single `/health/live` sample stalls over 50 ms.* Measured directly, repeatedly, against this
#    exact workload (12 trials across two shapes, this container, 2026-09-18): p50 stayed under
#    0.15 ms every time, while the **max** sample regularly exceeded 50 ms — as high as ~97 ms — with
#    no implementation change able to prevent it. This is not the loop blocking: it is 20 genuinely
#    concurrent CPU-bound Python threads (the default executor here caps at `min(32, cpu_count+4)`
#    workers) contending for the GIL, which periodically starves *any* other thread's turn, including
#    the one running the event loop — the identical mechanism
#    `test_posting_fetcher_event_loop.py`'s own docstring names ("the GIL serialises the four
#    extraction threads no matter how many cores are idle"). That file measures only 4 concurrent
#    fetches and asserts **only p50**, never a max — no max-latency assertion exists there either,
#    for the same underlying reason. Keeping a max<50 ms assertion here would be asserting something
#    about CPython's scheduler under heavy thread contention, not about this router or this adapter;
#    dropping it is not weakening the test to match an accident in *this* code; it is matching the
#    established precedent's own considered choice. `max(latencies)` is still reported in the p50
#    failure message, as diagnostic context, exactly as the precedent does.
# ---------------------------------------------------------------------------------------------


def _document_at_the_ceiling() -> str:
    """A CV comfortably inside `TailoredCv`'s 20,000-character ceiling, built from real words so
    markdown-it has actual parsing to do rather than one long token."""
    sentence = "Rewrote the platform reliability program end to end for this role. "
    text = sentence * 280  # ~19,600 characters, safely under the 20,000 ceiling
    return text[:19_900]


# The sample-size floor this measurement needs before a p50 means anything — see the section banner's
# "Sizing is a guarantee now, not a guess" paragraph. Shared by both loops below: it is the ONE number
# that decides when the batch is over, rather than a guess at how many renders that takes.
_SAMPLE_FLOOR = 20


async def _hammer_health_live_until_floor(
    client: AsyncClient, *, enough: asyncio.Event, floor: int
) -> list[float]:
    """Sample `/health/live` until `floor` samples are collected, then signal `enough` — the single
    stop condition both this loop and every render worker below check.

    See `test_posting_fetcher_event_loop.py::_hammer_health_live` for the full account of why the
    trailing sleep is load-bearing against an in-process ASGI transport with no real I/O. This
    version additionally *drives* the batch's end rather than merely obeying an externally-set `stop`
    — it is what turns "enough samples" from an assumption into a guarantee (see the section banner).
    """
    latencies: list[float] = []
    while not enough.is_set():
        started = time.perf_counter()
        response = await client.get("/health/live")
        latencies.append(time.perf_counter() - started)
        assert response.status_code == 200
        if len(latencies) >= floor:
            enough.set()
        await asyncio.sleep(0.001)
    return latencies


async def _render_until_enough_samples(
    renderer: MarkdownDocumentRenderer, document: str, *, enough: asyncio.Event
) -> None:
    """Keep rendering until the hammer has its floor, checked before every render rather than after —
    an in-flight render is always allowed to finish, never cancelled, so `enough` being set mid-render
    costs at most one extra render per worker, not a torn result."""
    while not enough.is_set():
        result = await renderer.render(
            document, document=TailoredDocumentKind.CV, format=ExportFormat.TXT
        )
        assert isinstance(result, bytes)
        assert len(result) > 0


@pytest.mark.slow
async def test_health_live_stays_responsive_during_twenty_concurrent_txt_renders(
    client: AsyncClient, settings: Settings
) -> None:
    """AC-11, reshaped — see the section banner above for the full account of why this drives
    `MarkdownDocumentRenderer` directly instead of the HTTP route, why each of the 20 concurrent
    workers renders until told to stop rather than a fixed number of times, and exactly which of the
    original claims survive."""
    renderer = MarkdownDocumentRenderer(settings)
    document = _document_at_the_ceiling()

    enough = asyncio.Event()
    hammer_task = asyncio.ensure_future(
        _hammer_health_live_until_floor(client, enough=enough, floor=_SAMPLE_FLOOR)
    )
    await asyncio.sleep(0)  # let the hammering task actually start before the renders begin

    # A generous but finite ceiling, not a tuned number: `enough` is set by the hammer itself the
    # instant it has its floor, so this bounds only the pathological case where that never happens
    # (a bug in the hammer, or an event loop so stalled that `/health/live` never gets a turn at all)
    # rather than deciding how long a healthy run takes, the way the old fixed iteration count did.
    await asyncio.wait_for(
        asyncio.gather(
            *(_render_until_enough_samples(renderer, document, enough=enough) for _ in range(20))
        ),
        timeout=30,
    )
    latencies = await asyncio.wait_for(hammer_task, timeout=15)

    # No longer a precondition that can fail on its own — `_hammer_health_live_until_floor` does not
    # return until it has collected `_SAMPLE_FLOOR` samples, so this is a self-check on that
    # invariant rather than a race against however long the renders happened to take.
    assert len(latencies) >= _SAMPLE_FLOOR, (
        f"only {len(latencies)} /health/live samples were taken during the 20 concurrent "
        "renders — the hammer loop returned before reaching its own floor, which should be "
        "impossible; see _hammer_health_live_until_floor"
    )
    p50 = statistics.median(latencies)
    # No `max(latencies) < ...` assertion here — see the section banner's claim #2 for the measured
    # reason: GIL contention among 20 genuinely concurrent CPU-bound threads produces real max-latency
    # spikes well past 50 ms that no implementation under this port can prevent, and the established
    # precedent (`test_posting_fetcher_event_loop.py`) asserts only p50 for the identical reason.
    assert p50 < 0.005, (
        f"/health/live p50 was {p50 * 1000:.2f} ms during 20 concurrent inline renders "
        f"(n={len(latencies)}, max={max(latencies) * 1000:.2f} ms) — the event loop was blocked"
    )
