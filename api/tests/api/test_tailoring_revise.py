"""API tests for `workspace-progress-and-editor`'s document-revision endpoint:
`PUT /api/tailoring-runs/{id}/documents/{kind}` (ADR-0015).

**This is the RED half of a red-first cycle** (CLAUDE.md, docs/sdlc.md §2, T13). Every test here is
written against `docs/specs/workspace-progress-and-editor/feature-spec.md`'s AC-10…AC-19 and its
failure-contract rows E-1…E-12, E-14, E-30, E-31 — not against
`infrastructure/api/routers/tailoring.py::revise_tailored_document`, whose body is currently a single
`raise NotImplementedError(...)` (T12's SKELETON). The router IS mounted, so a passing collection with
every test failing on a real assertion — never an `ImportError`, never "fixture not found" — is what a
correct RED run looks like here.

**Why a succeeded run is built directly through the aggregate and the repository, never through
`POST /api/tailoring-runs`.** T12's skeleton change also broke `POST`'s and `GET`'s own response
construction — `_to_response`/`_to_summary` now build a `TailoringRunResponse` that requires
`version`/`tailored_cv_edited_at`/`cover_letter_edited_at` without yet supplying them, so **every**
call to those two handlers 500s on a `pydantic.ValidationError` today (`test_tailoring.py`'s own new
AC-11 tests are the recorded red for exactly that). If this file's setup went through `POST`, every
test below would fail at that same, unrelated line instead of at the thing each test actually means to
guard — a red that does not discriminate is the `ImportError` problem one level up (CLAUDE.md, this
agent's brief). `_create_succeeded_run`/`_create_queued_run` instead build the run with
`TailoringRun.request` / `mark_started` / `mark_succeeded` / `mark_failed` and
`SqlAlchemyTailoringRunRepository`, directly on this test's own `session` — production code, called
rather than reimplemented, and untouched by AC-11's break because it never asks the router to render a
response. Uploading the base CV and the job posting still goes through the real HTTP surface (`intake`
and `posting` have their own, unaffected response schemas), so `_ready_inputs` still exercises those.

**Why nearly every behavioural assertion below fails at `assert 500 == <code>` today, and why that is
the correct red.** The `PUT` handler's only statement is the `raise`, so no precondition anyone sets up
(the run's status, its version, the rate limiter, the value object) changes what happens: every call
reaches the same `NotImplementedError` regardless. Four families of check are the exception, because
FastAPI and its middleware resolve them **before** the handler body ever runs, exactly as
`test_tailoring.py`'s own module docstring already found for `POST`/`GET`: the 256 KiB body cap (E-3,
ASGI middleware), the request's shape (`content`/`expected_version`/extra fields — E-1, E-2, Pydantic),
the path (a malformed run id or an unknown `kind` — E-4, FastAPI's own path typing) and the
guest-session cookie (E-5/AC-15, `require_guest_session`, a `Depends()` resolved ahead of the body).
Those four already answer their real code today; everything else in this file is the recorded red T14
turns green.

**This file duplicates `test_tailoring.py`'s HTTP seeding helpers rather than importing them.** Every
API test module in this codebase is self-contained this way (`test_posting.py` does not import from
`test_intake.py` either) — the shared, non-test module is `tests.integration.fakes`. `_committing_
session_override` is the one exception: it is imported from `tests.conftest`, because AC-12(b) below
needs a **second**, independently-overridden `FastAPI` app built the exact way `conftest.py`'s own
`app` fixture builds the first one, and re-deriving that docstring's reasoning here would risk silently
drifting from it.

**AC-12(b)'s "two writers racing through HTTP", and the one honest limitation in this file.**
`test_tailoring.py`'s module docstring already proves the constraint: a *genuinely separate* database
connection cannot see a run this suite's own setup just wrote, because `conftest.py`'s outer
transaction is rolled back at teardown and never actually committed at the Postgres wire level. A
second, truly independent Postgres transaction is therefore not available to this suite at all — not
just inconvenient. What this file does instead: a **second, independent `AsyncSession`** (its own
identity map), built from the *same* `async_sessionmaker` shape `conftest.py` uses, bound to the
**same already-open `connection`** the primary `session` fixture also uses. Both `Session` objects can
see the row `_create_succeeded_run` committed (same connection, same live transaction), but each holds
its **own** independent Python object for the run — which is exactly what a real concurrent request
would hold, and exactly what SQLAlchemy's `version_id_col` optimistic lock needs to have two loaded
copies to conflict over. The run is deliberately loaded into the second session's identity map
*before* the first writer moves the row on, and SQLAlchemy's default identity-map behaviour (a second
`SELECT` for an already-tracked primary key returns the cached object, unchanged, rather than
re-querying) is what keeps that copy stale until its own `save()` flushes and finds the row has moved.
This is not a claim that two literally-simultaneous requests were reproduced; it is the closest this
suite's own fixtures can get to it, and it is enough to exercise the real `StaleDataError` ->
`TailoringRunConcurrentlyModified` translation the mapping's `version_id_col` performs, rather than a
hand-rolled substitute for it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text as sql_text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

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
from tailorcraft.infrastructure.api.deps import get_clock, get_session, get_tailoring_queue
from tailorcraft.infrastructure.api.guest_session import COOKIE_NAME
from tailorcraft.infrastructure.api.main import create_app
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.settings import Settings
from tests.conftest import _committing_session_override
from tests.integration.fakes import FakeTailoringQueue

# ---------------------------------------------------------------------------------------------
# Fixture text — comfortably past every floor this slice's own value-object bounds must clear, and
# distinctive enough that the AC-19 privacy test can catch a *partial* leak. `TailoredCv`'s floor is
# 400 non-whitespace characters / 20,000-character ceiling; `CoverLetter`'s is 200 / 8,000
# (domain/tailoring/value_objects.py) — checked there rather than assumed, per this agent's brief.
# ---------------------------------------------------------------------------------------------


def _cv_body_text(marker: str = "QA_REVISE_FIXTURE_CV_TOKEN") -> str:
    filler = "Senior backend engineer with a decade leading platform reliability work. " * 8
    return f"{marker} {filler}"


def _posting_body_text(marker: str = "QA_REVISE_FIXTURE_POSTING_TOKEN") -> str:
    filler = "We are hiring a senior engineer to own reliability and mentor the team. " * 4
    return f"{marker} {filler}"


def _tailored_cv_text(marker: str) -> str:
    """Comfortably past `TailoredCv`'s 400-non-whitespace-character floor."""
    filler = "Rewrote the platform reliability program end to end for this role. " * 8
    return f"{marker} {filler}"


def _cover_letter_text(marker: str) -> str:
    """Comfortably past `CoverLetter`'s 200-non-whitespace-character floor."""
    filler = "I am applying because this role matches my reliability background. " * 6
    return f"{marker} {filler}"


# `TailoredCv`'s two bounds: floor 400 non-whitespace chars, ceiling 20,000 characters.
# `CoverLetter`'s: floor 200, ceiling 8,000 (domain/tailoring/value_objects.py).
_TOO_SHORT_CV = "a" * 399
_TOO_SHORT_LETTER = "b" * 199
_TOO_LONG_CV = "c" * 20_001
_TOO_LONG_LETTER = "d" * 8_001

_HOSTILE_SNIPPETS = [
    pytest.param("<script>alert(1)</script>", id="script-tag"),
    pytest.param("<img src=x onerror=alert(1)>", id="img-onerror"),
    pytest.param("[x](javascript:alert(1))", id="javascript-link"),
    pytest.param("![i](http://x/y.png)", id="markdown-image"),
]


def _now_whole_second() -> datetime:
    """`datetime.now()`, truncated to the whole-second contract (ADR-0007) — used only where this
    file builds a run directly rather than through the API's own `Clock` port, so the manual instant
    stays a real "now" and can never precede whatever real wall-clock instant an earlier step used."""
    return datetime.now(UTC).replace(microsecond=0)


# ---------------------------------------------------------------------------------------------
# Small HTTP helpers — mirror `test_tailoring.py`'s and `test_posting.py`'s
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
    from tailorcraft.infrastructure.api.deps import get_app_settings

    modified = base.model_copy(update=updates)
    app.dependency_overrides[get_app_settings] = lambda: modified
    app.state.settings = modified
    return modified


def _new_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
    )


# ---------------------------------------------------------------------------------------------
# Seeding — CV/posting through the real, already-shipped HTTP surfaces (1.1, 1.2); the run itself
# built directly through the aggregate and the repository (see the module docstring for why).
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
    """The owning session's id, read off the base CV this client just uploaded — avoids depending
    on any particular cookie encoding to recover it."""
    result = await session.execute(
        sql_text("SELECT guest_session_id FROM intake_base_cv WHERE id = :id"),
        {"id": UUID(base_cv_id)},
    )
    return UUID(str(result.scalar_one()))


def _repo(session: AsyncSession) -> object:
    # Deferred import: this module reads `TailoringRun._id` etc. at import time to build its typed
    # column references, which only exist once `configure_mappings()` (conftest.py's session-scoped
    # `_mappings` fixture) has run — true by the time any test body executes, but not yet true at
    # collection time if this were a module-level import.
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
    from tailorcraft.domain.tailoring.tailoring_run import TailoringRun  # noqa: F401

    run_id = await _create_queued_run(client, session)
    repo = _repo(session)
    run = await repo.get(TailoringRunId(UUID(run_id)))  # type: ignore[attr-defined]
    run.mark_started(_now_whole_second())
    await repo.save(run)  # type: ignore[attr-defined]
    await session.commit()
    return run_id


async def _create_failed_run(client: AsyncClient, session: AsyncSession) -> str:
    run_id = await _create_queued_run(client, session)
    repo = _repo(session)
    run = await repo.get(TailoringRunId(UUID(run_id)))  # type: ignore[attr-defined]
    run.mark_started(_now_whole_second())
    run.mark_failed(TailoringFailureReason.LLM_REFUSED, _now_whole_second())
    await repo.save(run)  # type: ignore[attr-defined]
    await session.commit()
    return run_id


async def _create_succeeded_run(
    client: AsyncClient,
    session: AsyncSession,
    *,
    cv_text: str | None = None,
    posting_text: str | None = None,
    cv_marker: str = "QA_REVISE_DRAFT_CV_TOKEN",
    letter_marker: str = "QA_REVISE_DRAFT_LETTER_TOKEN",
) -> str:
    run_id = await _advance_to_running(client, session)
    repo = _repo(session)
    run = await repo.get(TailoringRunId(UUID(run_id)))  # type: ignore[attr-defined]
    run.mark_succeeded(
        TailoredDocuments(
            cv=TailoredCv(_tailored_cv_text(cv_marker)),
            cover_letter=CoverLetter(_cover_letter_text(letter_marker)),
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


def _install_queue(app: FastAPI) -> FakeTailoringQueue:
    """Unused by any run built in this file (they never go through `POST`), kept only so a test
    that wants to prove a route is untouched by the revise endpoint can still install it. Currently
    unused; retained for symmetry with `test_tailoring.py`."""
    queue = FakeTailoringQueue()
    app.dependency_overrides[get_tailoring_queue] = lambda: queue
    return queue


# ---------------------------------------------------------------------------------------------
# Raw-SQL checks — used instead of `GET` wherever a test would otherwise depend on AC-11's own red
# (the GETs 500 today on response validation; see `test_tailoring.py`'s new AC-11 tests). Reading
# the row directly keeps each test here about the `PUT` handler alone.
# ---------------------------------------------------------------------------------------------


async def _row_version(session: AsyncSession, run_id: str) -> int:
    result = await session.execute(
        sql_text("SELECT version FROM tailoring_run WHERE id = :id"), {"id": UUID(run_id)}
    )
    return int(result.scalar_one())


async def _row_documents(
    session: AsyncSession, run_id: str
) -> tuple[str | None, str | None, datetime | None, datetime | None]:
    result = await session.execute(
        sql_text(
            "SELECT edited_cv, edited_cover_letter, cv_edited_at, cover_letter_edited_at "
            "FROM tailoring_run WHERE id = :id"
        ),
        {"id": UUID(run_id)},
    )
    row = result.one()
    return row.edited_cv, row.edited_cover_letter, row.cv_edited_at, row.cover_letter_edited_at


async def _count_guest_sessions(session: AsyncSession) -> int:
    result = await session.execute(sql_text("SELECT count(*) FROM identity_guest_session"))
    return int(result.scalar_one())


# ---------------------------------------------------------------------------------------------
# Module-local fixtures
# ---------------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Shadows `conftest.py`'s `client` fixture: `raise_app_exceptions=False` turns the skeleton's
    `NotImplementedError` into a real `500` response instead of a bare Python exception — see the
    module docstring."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """`tailoring:revise` is a new Redis namespace, untouched by the per-test transaction rollback."""
    return None


# ---------------------------------------------------------------------------------------------
# AC-10 — a succeeded run's own document is replaced; the other is untouched; version bumps;
# `{kind}_edited_at` is set; counts are recomputed; `Cache-Control: no-store`.
# ---------------------------------------------------------------------------------------------


async def test_revising_the_cv_returns_200_with_bumped_version_and_current_documents(
    client: AsyncClient, session: AsyncSession
) -> None:
    run_id = await _create_succeeded_run(client, session)
    version_before = await _row_version(session, run_id)
    new_cv = _tailored_cv_text("QA_REVISE_NEW_CV_TOKEN")

    response = await client.put(
        f"/api/tailoring-runs/{run_id}/documents/cv",
        json={"content": new_cv, "expected_version": version_before},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == version_before + 1
    assert body["tailored_cv"] == new_cv
    assert "QA_REVISE_DRAFT_LETTER_TOKEN" in body["cover_letter"], (
        "the other document must be untouched by a CV revision"
    )
    assert body["tailored_cv_character_count"] == len(new_cv)
    assert body["tailored_cv_edited_at"] is not None
    assert body["cover_letter_edited_at"] is None
    assert response.headers.get("cache-control") == "no-store"


async def test_revising_the_cover_letter_returns_200_with_bumped_version_and_current_documents(
    client: AsyncClient, session: AsyncSession
) -> None:
    run_id = await _create_succeeded_run(client, session)
    version_before = await _row_version(session, run_id)
    new_letter = _cover_letter_text("QA_REVISE_NEW_LETTER_TOKEN")

    response = await client.put(
        f"/api/tailoring-runs/{run_id}/documents/cover_letter",
        json={"content": new_letter, "expected_version": version_before},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == version_before + 1
    assert body["cover_letter"] == new_letter
    assert "QA_REVISE_DRAFT_CV_TOKEN" in body["tailored_cv"], (
        "the other document must be untouched by a cover-letter revision"
    )
    assert body["cover_letter_character_count"] == len(new_letter)
    assert body["cover_letter_edited_at"] is not None
    assert body["tailored_cv_edited_at"] is None
    assert response.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------------------------
# AC-12(a) — the domain-level conflict: a stale `expected_version` answers 409
# `document_version_conflict` with `current_version`, and writes nothing.
# ---------------------------------------------------------------------------------------------


async def test_stale_expected_version_returns_409_with_current_version_and_writes_nothing(
    client: AsyncClient, session: AsyncSession
) -> None:
    run_id = await _create_succeeded_run(client, session)
    version_before = await _row_version(session, run_id)

    response = await client.put(
        f"/api/tailoring-runs/{run_id}/documents/cv",
        json={
            "content": _tailored_cv_text("QA_REVISE_SHOULD_NOT_BE_STORED"),
            "expected_version": version_before + 1,
        },
    )

    assert response.status_code == 409, response.text
    assert _error_code(response) == "document_version_conflict"
    assert response.json()["error"]["current_version"] == version_before
    assert await _row_version(session, run_id) == version_before, "AC-12: nothing may be written"
    edited_cv, _, _, _ = await _row_documents(session, run_id)
    assert edited_cv is None or "QA_REVISE_SHOULD_NOT_BE_STORED" not in edited_cv


# ---------------------------------------------------------------------------------------------
# AC-12(b) — the database-level race, driven through HTTP over two independent `AsyncSession`s (see
# the module docstring for exactly what this does and does not reproduce).
# ---------------------------------------------------------------------------------------------


async def test_two_writers_racing_through_http_second_answers_409_with_null_current_version(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    connection: AsyncConnection,
    settings: Settings,
) -> None:
    from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
        SqlAlchemyTailoringRunRepository,
    )

    run_id = await _create_succeeded_run(client, session)
    run_uuid = TailoringRunId(UUID(run_id))
    version = await _row_version(session, run_id)
    # Close the transaction the raw `SELECT` above just opened on `session` (autobegin), so it is
    # not left dangling *underneath* the nested SAVEPOINT `session2` is about to open on this same
    # connection — two nested transactions on one connection must close in strict LIFO order, and a
    # read-only leftover here would make `session`'s later close the OUTER one while `session2`'s is
    # still open, which is exactly backwards.
    await session.commit()

    # A second, independent `AsyncSession` on the SAME already-open connection as `session` — see
    # the module docstring for why a second, genuinely separate connection is not available here.
    second_factory = async_sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    session2 = second_factory()
    repo2 = SqlAlchemyTailoringRunRepository(session2)
    # Pre-load the run into session2's identity map NOW, before the first writer moves the row on.
    # A later query for this same primary key inside `session2` returns this cached copy rather
    # than re-querying (SQLAlchemy's default identity-map behaviour) — the staleness a genuinely
    # concurrent second request would also hold.
    await repo2.get(run_uuid)

    app2 = create_app(settings)
    app2.dependency_overrides[get_session] = _committing_session_override(session2)
    app2.state.settings = settings
    app2.state.engine = app.state.engine
    app2.state.session_factory = lambda: session2
    app2.state.celery = app.state.celery

    cookie = client.cookies.get(COOKIE_NAME)
    assert cookie is not None
    client2 = _new_client(app2)
    client2.cookies.set(COOKIE_NAME, cookie)

    try:
        first = await client.put(
            f"/api/tailoring-runs/{run_id}/documents/cv",
            json={"content": _tailored_cv_text("QA_RACE_WINNER"), "expected_version": version},
        )
        second = await client2.put(
            f"/api/tailoring-runs/{run_id}/documents/cv",
            json={"content": _tailored_cv_text("QA_RACE_LOSER"), "expected_version": version},
        )
    finally:
        await client2.aclose()

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert _error_code(second) == "document_version_conflict"
    assert second.json()["error"]["current_version"] is None, (
        "AC-12(b): the aggregate the second writer holds is the stale copy; the true number lives "
        "in a row it has just been told it does not have"
    )

    edited_cv, _, _, _ = await _row_documents(session, run_id)
    assert edited_cv is not None
    assert "QA_RACE_WINNER" in edited_cv
    assert "QA_RACE_LOSER" not in edited_cv


# ---------------------------------------------------------------------------------------------
# AC-13 — a run that is not `succeeded` answers 409 `tailoring_run_not_editable` with `status`;
# nothing is written. All three other statuses.
# ---------------------------------------------------------------------------------------------


async def test_revising_a_queued_run_returns_409_tailoring_run_not_editable(
    client: AsyncClient, session: AsyncSession
) -> None:
    run_id = await _create_queued_run(client, session)

    response = await client.put(
        f"/api/tailoring-runs/{run_id}/documents/cv",
        json={"content": _tailored_cv_text("QA_REVISE_QUEUED_ATTEMPT"), "expected_version": 1},
    )

    assert response.status_code == 409, response.text
    assert _error_code(response) == "tailoring_run_not_editable"
    assert response.json()["error"]["status"] == "queued"
    edited_cv, edited_letter, _, _ = await _row_documents(session, run_id)
    assert edited_cv is None
    assert edited_letter is None


async def test_revising_a_running_run_returns_409_tailoring_run_not_editable(
    client: AsyncClient, session: AsyncSession
) -> None:
    run_id = await _advance_to_running(client, session)

    response = await client.put(
        f"/api/tailoring-runs/{run_id}/documents/cv",
        json={"content": _tailored_cv_text("QA_REVISE_RUNNING_ATTEMPT"), "expected_version": 2},
    )

    assert response.status_code == 409, response.text
    assert _error_code(response) == "tailoring_run_not_editable"
    assert response.json()["error"]["status"] == "running"
    edited_cv, edited_letter, _, _ = await _row_documents(session, run_id)
    assert edited_cv is None
    assert edited_letter is None


async def test_revising_a_failed_run_returns_409_tailoring_run_not_editable(
    client: AsyncClient, session: AsyncSession
) -> None:
    run_id = await _create_failed_run(client, session)

    response = await client.put(
        f"/api/tailoring-runs/{run_id}/documents/cv",
        json={"content": _tailored_cv_text("QA_REVISE_FAILED_ATTEMPT"), "expected_version": 3},
    )

    assert response.status_code == 409, response.text
    assert _error_code(response) == "tailoring_run_not_editable"
    assert response.json()["error"]["status"] == "failed"
    edited_cv, edited_letter, _, _ = await _row_documents(session, run_id)
    assert edited_cv is None
    assert edited_letter is None


# ---------------------------------------------------------------------------------------------
# AC-14 — a run belonging to another session, and a nonexistent id, are byte-identical 404s.
# ---------------------------------------------------------------------------------------------


async def test_revising_another_sessions_run_and_a_nonexistent_id_return_byte_identical_404(
    client: AsyncClient, app: FastAPI, session: AsyncSession
) -> None:
    run_id = await _create_succeeded_run(client, session)

    async with _new_client(app) as other_client:
        await _mint_cookie(other_client)
        not_mine = await other_client.put(
            f"/api/tailoring-runs/{run_id}/documents/cv",
            json={"content": _tailored_cv_text("QA_REVISE_FOREIGN_ATTEMPT"), "expected_version": 3},
        )
        never_existed = await other_client.put(
            f"/api/tailoring-runs/{uuid4()}/documents/cv",
            json={"content": _tailored_cv_text("QA_REVISE_FOREIGN_ATTEMPT"), "expected_version": 3},
        )

    assert not_mine.status_code == never_existed.status_code == 404
    assert _error_code(not_mine) == _error_code(never_existed) == "tailoring_run_not_found"
    assert not_mine.text == never_existed.text, "AC-14: the same 404 body for both"


# ---------------------------------------------------------------------------------------------
# AC-15 / E-5 — missing, unknown or expired cookie: 401, no `Set-Cookie`, no new session row.
# ---------------------------------------------------------------------------------------------


async def test_cookieless_put_returns_401_mints_no_session_and_creates_no_row(
    client: AsyncClient, session: AsyncSession
) -> None:
    before = await _count_guest_sessions(session)

    response = await client.put(
        f"/api/tailoring-runs/{uuid4()}/documents/cv",
        json={"content": _tailored_cv_text("QA_REVISE_NO_COOKIE"), "expected_version": 1},
    )

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"
    assert _guest_cookie_header(response) is None, "AC-15: this PUT must never mint a session"
    after = await _count_guest_sessions(session)
    assert after == before, "AC-15: a cookieless PUT must create no identity_guest_session row"


async def test_unknown_cookie_on_put_returns_401(client: AsyncClient) -> None:
    client.cookies.set(COOKIE_NAME, "a-token-that-was-never-minted-by-this-server")

    response = await client.put(
        f"/api/tailoring-runs/{uuid4()}/documents/cv",
        json={"content": _tailored_cv_text("QA_REVISE_UNKNOWN_COOKIE"), "expected_version": 1},
    )

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


async def test_expired_cookie_on_put_returns_401(client: AsyncClient, app: FastAPI) -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    app.dependency_overrides[get_clock] = lambda: clock
    await _mint_cookie(client)

    clock.advance(25 * 3600)  # past the default 24h retention window

    response = await client.put(
        f"/api/tailoring-runs/{uuid4()}/documents/cv",
        json={"content": _tailored_cv_text("QA_REVISE_EXPIRED_COOKIE"), "expected_version": 1},
    )

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


# ---------------------------------------------------------------------------------------------
# AC-16 / E-10, E-11, E-12 — content that fails the value object: 422 `document_invalid` with a
# fixed `problem`, and the offending text never in the response body. Nothing is written.
# ---------------------------------------------------------------------------------------------

_INVALID_CONTENT_CASES = [
    pytest.param("cv", "   ", "empty", id="cv-empty"),
    pytest.param("cv", _TOO_SHORT_CV, "too_short", id="cv-too-short"),
    pytest.param("cv", _TOO_LONG_CV, "too_long", id="cv-too-long"),
    pytest.param("cv", "valid enough words\x00with a nul", "invalid_characters", id="cv-nul"),
    pytest.param("cover_letter", "   ", "empty", id="letter-empty"),
    pytest.param("cover_letter", _TOO_SHORT_LETTER, "too_short", id="letter-too-short"),
    pytest.param("cover_letter", _TOO_LONG_LETTER, "too_long", id="letter-too-long"),
    pytest.param(
        "cover_letter", "valid enough words\x07with a bel", "invalid_characters", id="letter-bel"
    ),
]


@pytest.mark.parametrize(("kind", "content", "problem"), _INVALID_CONTENT_CASES)
async def test_invalid_content_returns_422_document_invalid_without_leaking_the_text(
    client: AsyncClient,
    session: AsyncSession,
    kind: str,
    content: str,
    problem: str,
) -> None:
    run_id = await _create_succeeded_run(client, session)
    version_before = await _row_version(session, run_id)

    response = await client.put(
        f"/api/tailoring-runs/{run_id}/documents/{kind}",
        json={"content": content, "expected_version": version_before},
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "document_invalid"
    assert response.json()["error"]["problem"] == problem
    assert content.strip() not in response.text or content.isspace(), (
        "AC-16: the offending text must never appear in the response body"
    )
    assert await _row_version(session, run_id) == version_before, "AC-16: nothing may be written"


# ---------------------------------------------------------------------------------------------
# AC-17 — raw HTML, a `<script>`, a `javascript:` link and a Markdown image are accepted as text
# and stored/served verbatim.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("snippet", _HOSTILE_SNIPPETS)
async def test_hostile_markup_in_content_is_accepted_and_stored_verbatim(
    client: AsyncClient, session: AsyncSession, snippet: str
) -> None:
    run_id = await _create_succeeded_run(client, session)
    version_before = await _row_version(session, run_id)
    content = f"{_tailored_cv_text('QA_REVISE_HOSTILE')}\n\n{snippet}"

    response = await client.put(
        f"/api/tailoring-runs/{run_id}/documents/cv",
        json={"content": content, "expected_version": version_before},
    )

    assert response.status_code == 200, response.text
    assert snippet in response.json()["tailored_cv"], (
        "AC-17: hostile markup is accepted as text and returned character for character"
    )
    edited_cv, _, _, _ = await _row_documents(session, run_id)
    assert edited_cv is not None
    assert snippet in edited_cv


# ---------------------------------------------------------------------------------------------
# AC-18 / E-30 — the save rate limit (`tailoring:revise`, fail-open): exhausted -> 429 +
# `Retry-After`; nothing is written.
# ---------------------------------------------------------------------------------------------


async def test_exceeding_the_revise_rate_limit_returns_429_and_writes_nothing(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    run_id = await _create_succeeded_run(client, session)
    version_before = await _row_version(session, run_id)
    _override_settings(app, settings, tailoring_revise_rate_limit_per_hour=2)

    first = await client.put(
        f"/api/tailoring-runs/{run_id}/documents/cv",
        json={"content": _tailored_cv_text("QA_REVISE_RL_1"), "expected_version": version_before},
    )
    second = await client.put(
        f"/api/tailoring-runs/{run_id}/documents/cv",
        json={"content": _tailored_cv_text("QA_REVISE_RL_2"), "expected_version": version_before},
    )
    third = await client.put(
        f"/api/tailoring-runs/{run_id}/documents/cv",
        json={"content": _tailored_cv_text("QA_REVISE_RL_3"), "expected_version": version_before},
    )

    assert first.status_code != 429, first.text
    assert second.status_code != 429, second.text
    assert third.status_code == 429, third.text
    assert _error_code(third) == "rate_limited"
    assert "retry-after" in {name.lower() for name in third.headers}
    edited_cv, _, _, _ = await _row_documents(session, run_id)
    assert edited_cv is None or "QA_REVISE_RL_3" not in edited_cv


# ---------------------------------------------------------------------------------------------
# E-1, E-2, E-4 — malformed body or path: 422 `validation_error`. Requires a valid cookie: with no
# cookie, `require_guest_session` fires before FastAPI's own body/path validation (the same finding
# `test_tailoring.py` documents for POST/GET).
# ---------------------------------------------------------------------------------------------


async def test_body_that_is_not_valid_json_returns_422_validation_error(
    client: AsyncClient,
) -> None:
    await _mint_cookie(client)

    response = await client.put(
        f"/api/tailoring-runs/{uuid4()}/documents/cv",
        content=b"not json at all",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


async def test_empty_body_returns_422_validation_error(client: AsyncClient) -> None:
    await _mint_cookie(client)

    response = await client.put(
        f"/api/tailoring-runs/{uuid4()}/documents/cv",
        content=b"",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"expected_version": 1}, id="content-missing"),
        pytest.param({"content": 12345, "expected_version": 1}, id="content-not-a-string"),
        pytest.param({"content": "fine"}, id="expected_version-missing"),
        pytest.param({"content": "fine", "expected_version": 0}, id="expected_version-zero"),
        pytest.param(
            {"content": "fine", "expected_version": "one"}, id="expected_version-not-an-int"
        ),
        pytest.param(
            {"content": "fine", "expected_version": 1, "extra": "nope"}, id="extra-field-forbidden"
        ),
    ],
)
async def test_malformed_body_shape_returns_422_validation_error(
    client: AsyncClient, body: dict[str, object]
) -> None:
    await _mint_cookie(client)

    response = await client.put(f"/api/tailoring-runs/{uuid4()}/documents/cv", json=body)

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


async def test_unknown_kind_path_segment_returns_422(client: AsyncClient) -> None:
    await _mint_cookie(client)

    response = await client.put(
        f"/api/tailoring-runs/{uuid4()}/documents/bogus",
        json={"content": "fine", "expected_version": 1},
    )

    assert response.status_code == 422, response.text


async def test_malformed_run_id_in_path_returns_422(client: AsyncClient) -> None:
    await _mint_cookie(client)

    response = await client.put(
        "/api/tailoring-runs/not-a-uuid/documents/cv",
        json={"content": "fine", "expected_version": 1},
    )

    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------------------------
# E-3 — the 256 KiB JSON body cap, refused before parsing (no cookie needed: the middleware runs
# before dependency resolution).
# ---------------------------------------------------------------------------------------------


async def test_put_body_over_the_cap_returns_413_request_too_large(
    client: AsyncClient, settings: Settings
) -> None:
    padding = "a" * (settings.json_request_max_bytes + 1024)
    body = ('{"content":"' + padding + '","expected_version":1}').encode()
    assert len(body) > settings.json_request_max_bytes

    response = await client.put(
        f"/api/tailoring-runs/{uuid4()}/documents/cv",
        content=body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413, response.text
    assert _error_code(response) == "request_too_large"


# ---------------------------------------------------------------------------------------------
# E-14 — Postgres down, or the commit fails: 503 `service_unavailable`; nothing survives.
# ---------------------------------------------------------------------------------------------


async def test_commit_failure_returns_503_service_unavailable(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await _create_succeeded_run(client, session)
    version_before = await _row_version(session, run_id)

    async def _raise_sqlalchemy_error() -> None:
        raise SQLAlchemyError("simulated commit failure")

    monkeypatch.setattr(session, "commit", _raise_sqlalchemy_error)

    response = await client.put(
        f"/api/tailoring-runs/{run_id}/documents/cv",
        json={
            "content": _tailored_cv_text("QA_REVISE_SHOULD_NOT_SURVIVE"),
            "expected_version": version_before,
        },
    )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"


# ---------------------------------------------------------------------------------------------
# E-31 — Redis unreachable: the save limiter fails OPEN, so the save still succeeds.
# ---------------------------------------------------------------------------------------------


async def test_revise_rate_limiter_fails_open_when_redis_is_unreachable(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    run_id = await _create_succeeded_run(client, session)
    version_before = await _row_version(session, run_id)
    _override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await client.put(
        f"/api/tailoring-runs/{run_id}/documents/cv",
        json={
            "content": _tailored_cv_text("QA_REVISE_FAIL_OPEN"),
            "expected_version": version_before,
        },
    )

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------------------------
# AC-19 — no fragment of `content` ever appears in a log line, on the happy path or on any 4xx.
# Six error shapes plus one success, captured together: 409 (not_editable, version_conflict),
# 422 (validation_error, document_invalid), 404, 401, 429.
# ---------------------------------------------------------------------------------------------


async def test_revise_privacy_no_fragment_of_content_ever_logged(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    success_marker = "QA_REVISE_PRIVACY_SUCCESS_TOKEN"
    conflict_marker = "QA_REVISE_PRIVACY_CONFLICT_TOKEN"
    invalid_marker = "QA_REVISE_PRIVACY_INVALID_TOKEN_" + "z" * 400  # too long, never saved

    with caplog.at_level(logging.INFO):
        run_id = await _create_succeeded_run(client, session)
        version_before = await _row_version(session, run_id)

        success = await client.put(
            f"/api/tailoring-runs/{run_id}/documents/cv",
            json={"content": _tailored_cv_text(success_marker), "expected_version": version_before},
        )

        queued_run_id = await _create_queued_run(client, session)
        not_editable = await client.put(
            f"/api/tailoring-runs/{queued_run_id}/documents/cv",
            json={
                "content": _tailored_cv_text("QA_REVISE_PRIVACY_NOT_EDITABLE"),
                "expected_version": 1,
            },
        )

        conflict = await client.put(
            f"/api/tailoring-runs/{run_id}/documents/cv",
            json={"content": _tailored_cv_text(conflict_marker), "expected_version": 1},
        )

        validation_error = await client.put(
            f"/api/tailoring-runs/{run_id}/documents/cv",
            content=b"not json at all",
            headers={"content-type": "application/json"},
        )

        document_invalid = await client.put(
            f"/api/tailoring-runs/{run_id}/documents/cv",
            json={"content": invalid_marker, "expected_version": version_before},
        )

        not_found = await client.put(
            f"/api/tailoring-runs/{uuid4()}/documents/cv",
            json={"content": _tailored_cv_text("QA_REVISE_PRIVACY_404"), "expected_version": 1},
        )

        async with _new_client(app) as cookieless_client:
            expired = await cookieless_client.put(
                f"/api/tailoring-runs/{run_id}/documents/cv",
                json={"content": _tailored_cv_text("QA_REVISE_PRIVACY_401"), "expected_version": 1},
            )

        _override_settings(app, settings, tailoring_revise_rate_limit_per_hour=1)
        rate_limited = await client.put(
            f"/api/tailoring-runs/{run_id}/documents/cv",
            json={
                "content": _tailored_cv_text("QA_REVISE_PRIVACY_429"),
                "expected_version": version_before,
            },
        )

    log_output = caplog.text

    # The positive proof: AC-19 requires `tailoring_run_id` to be logged on the success path, so
    # its presence is what proves the channel these negative assertions rely on was captured at
    # all, not merely silent because nothing tailoring-specific happened.
    assert run_id in log_output, (
        "expected the successful save's tailoring_run_id to appear in the logs (AC-19) — its "
        "absence means the channel was never captured and the assertions below pass vacuously"
    )

    for marker in (success_marker, conflict_marker, invalid_marker):
        assert marker not in log_output, f"{marker!r} leaked into the logs"

    # Every response, whatever its status today, must never carry the content either.
    for response in (
        success,
        not_editable,
        conflict,
        validation_error,
        document_invalid,
        not_found,
        expired,
        rate_limited,
    ):
        for marker in (success_marker, conflict_marker, invalid_marker):
            assert marker not in response.text, f"{marker!r} leaked into a response body"
