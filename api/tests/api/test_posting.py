"""API tests for `posting-job-description-intake`'s HTTP surface: `POST/GET /api/job-postings[/{id}]`.

**This is the RED half of a red-first cycle** (CLAUDE.md, sdlc.md §2, T26). Every test here is written
against `docs/specs/posting-job-description-intake/feature-spec.md`'s failure contract (P-1…P-37) and
its acceptance criteria — not against `infrastructure/api/routers/posting.py`, whose three handlers
currently do nothing but `raise NotImplementedError` (T25's SKELETON). The router IS mounted
(`create_app` includes it), so a passing collection with every test failing on a real assertion —
never a 404, never an ImportError — is exactly what a correct RED run looks like here.

**Why this file's `client` fixture is not the shared one.** Same reason as `test_intake.py`, restated
for this router: `tests/conftest.py`'s `client` uses `ASGITransport(app=app)`, whose default
`raise_app_exceptions=True` re-raises an unhandled handler exception into the test as a bare Python
exception rather than a real HTTP response. That turns every `NotImplementedError` skeleton into an
ERROR, not a `FAIL`, and buries the assertion this file is built around. This module's own `client`
fixture sets `raise_app_exceptions=False`, so `response.status_code` is a real `500` and
`assert response.status_code == 201` fails exactly the way CLAUDE.md wants a red to fail: on the
assertion (`assert 500 == 201`).

**No test in this suite makes a real outbound HTTP request.** `JobPostingFetcherPort` is replaced in
every test that needs one through `app.dependency_overrides[get_job_posting_fetcher]`, the same rule
ADR-0004 sets for the LLM and for the identical reasons: slow, flaky, someone else's rate limit, and
the failure paths are exactly the ones that matter most here. `_FakeJobPostingFetcher` below is
configured with either a `FetchedPosting` to return or a `JobPostingFetchFailed` to raise.

**Why the GET tests seed their fixture data directly through the repository/application layer,
instead of via `POST`.** `POST /api/job-postings` is itself one of the three unimplemented handlers,
so a GET-side test that first tries to `POST` a fixture posting would fail at that setup step rather
than at the assertion the test is actually named for — every GET test would read as "creation is
broken", which is true but says nothing about whether `GET`'s own authorization, preview or
cache-control behaviour is right. `_mint_guest_session` and `_seed_posting` build a real
`GuestSession` and a real `JobPosting` straight through `StartGuestSession` and
`SqlAlchemyJobPostingRepository` — production code, called rather than modified — so the three GET
tests exercise the actual handler under test and fail on their own, real assertion instead of
borrowing POST's failure.

**Why `clear_redis` rides on every test that posts.** Two new Redis namespaces, `posting:create` and
`posting:fetch`, are not touched by the per-test transaction rollback (CLAUDE.md). An autouse fixture
applies it to every test in this module, mirroring `test_intake.py`'s `_reset_redis_between_tests`.

**AC-18's privacy test uses `caplog`, not `structlog.testing.capture_logs()`, for the same
empirically-verified reason `test_intake.py`'s module docstring documents at length**: a module-level
logger cached from an earlier test in this session holds a stale reference to that test's processors
list, which `capture_logs()`'s in-place mutation cannot reach. `caplog` sidesteps the question
entirely by reading the rendered string off the root logger's handler, regardless of which processors
chain produced it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text as sql_text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.identity.start_guest_session import StartGuestSession
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.posting.errors import (
    JobPostingFetchFailed,
    SourceHasNoReadableText,
    SourceNotHtml,
    SourceRejectedRequest,
    SourceResponseTooLarge,
    SourceTextTooLong,
    SourceTimedOut,
    SourceTooManyRedirects,
    SourceUnreachable,
    SourceUrlNotAllowed,
)
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import (
    FetchedPosting,
    FetchFailureReason,
    JobPostingText,
    PostingTitle,
    SourceUrl,
)
from tailorcraft.infrastructure.api.deps import (
    get_app_settings,
    get_clock,
    get_job_posting_fetcher,
)
from tailorcraft.infrastructure.api.errors import _fetch_failure_to_http
from tailorcraft.infrastructure.api.guest_session import (
    COOKIE_NAME,
    MintedGuestToken,
    mint_guest_token,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.settings import Settings

# ---------------------------------------------------------------------------------------------
# Fixture text and URLs
# ---------------------------------------------------------------------------------------------

# Deliberately padded with extra internal whitespace and blank lines so AC-1's "character_count
# equals the length of the whitespace-normalized text" is checkable against an independently
# computed expectation (below), not against whatever the code happens to produce.
PASTED_TEXT_RAW = (
    "  We are looking   for a Senior  Python Engineer to join our platform team.\n\n"
    "Responsibilities include:\n"
    "  - Designing and building backend services\n"
    "  - Mentoring two junior engineers\n"
    "  - Owning the on-call rotation for our core APIs\n\n"
    "Requirements:\n"
    "  - Five years of professional Python experience\n"
    "  - Strong SQL and relational database skills\n"
    "  - Experience working in a distributed, service-oriented architecture\n\n"
    "We offer remote work, a flexible schedule, and a generous annual learning budget.   \n"
)
# `JobPostingText`'s own documented algorithm (`" ".join(value.split())`) — computed here
# independently of the router under test, per CLAUDE.md: a test encodes what the code should do,
# never what it was observed doing.
_EXPECTED_PASTED_NORMALIZED_TEXT = " ".join(PASTED_TEXT_RAW.split())
_EXPECTED_PASTED_CHARACTER_COUNT = len(_EXPECTED_PASTED_NORMALIZED_TEXT)

# Already single-spaced and free of leading/trailing whitespace, so it equals its own normalized
# form — `character_count` can be asserted as `len(_FETCH_FIXTURE_TEXT)` directly.
_FETCH_FIXTURE_TEXT = (
    "We are hiring a senior platform engineer to own service reliability, mentor two engineers, "
    "and help shape our roadmap. Five years of Python experience and strong SQL skills are "
    "required. Remote work is available with a flexible schedule and a generous annual learning "
    "budget for every engineer on the team."
)
_FETCH_FIXTURE_TITLE = "Senior Platform Engineer"

# AC-18's fixture: a distinctive path and query so the assertions below are checking a fact
# specific to this posting, not a coincidental absence.
_PRIVACY_FIXTURE_URL = "https://jobs.example.com/secret-path-abc123?ref=xyz789"
_PRIVACY_FIXTURE_IP = (
    "203.0.113.55"  # TEST-NET-3 (RFC 5737) — safe to use in a test, never routable
)
_PRIVACY_FIXTURE_TEXT_MARKER = "ACME_WIDGETS_FIXTURE_TOKEN_7f3a91"
_PRIVACY_FIXTURE_TEXT = (
    "We are hiring a senior platform engineer. "
    + _PRIVACY_FIXTURE_TEXT_MARKER
    + " You will own service reliability, mentor two engineers, and help shape our roadmap. Five "
    "years of Python experience and strong SQL skills are required, and remote work is available "
    "with a flexible schedule and a generous annual learning budget for every engineer."
)


# ---------------------------------------------------------------------------------------------
# Small HTTP helpers — mirror test_intake.py's
# ---------------------------------------------------------------------------------------------


def _error_code(response: Response) -> str:
    body = response.json()
    assert "error" in body, f"expected the {{'error': {{...}}}} envelope, got {body!r}"
    return str(body["error"]["code"])


def _error_message(response: Response) -> str:
    body = response.json()
    assert "error" in body, f"expected the {{'error': {{...}}}} envelope, got {body!r}"
    return str(body["error"]["message"])


def _guest_cookie_header(response: Response) -> str | None:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{COOKIE_NAME}="):
            return header
    return None


def _guest_cookie_value(response: Response) -> str | None:
    header = _guest_cookie_header(response)
    if header is None:
        return None
    return header.split(";", 1)[0].split("=", 1)[1]


def _override_settings(app: FastAPI, base: Settings, **updates: object) -> Settings:
    """Point every settings-reading path at one modified `Settings` — see `test_intake.py`'s twin."""
    modified = base.model_copy(update=updates)
    app.dependency_overrides[get_app_settings] = lambda: modified
    app.state.settings = modified
    return modified


def _new_client(app: FastAPI) -> AsyncClient:
    """A second, independent cookie jar against the same app, seen by the server as the same peer
    IP — for the per-IP fetch rate-limit test (P-33)."""
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver"
    )


# ---------------------------------------------------------------------------------------------
# The fake JobPostingFetcherPort — no test in this suite ever makes a real outbound request.
# ---------------------------------------------------------------------------------------------


@dataclass
class _FakeJobPostingFetcher:
    """Stands in for `JobPostingFetcherPort.fetch`. Configured with either a `FetchedPosting` to
    return, or a `JobPostingFetchFailed` to raise — never both."""

    posting: FetchedPosting | None = None
    failure: JobPostingFetchFailed | None = None
    requested_url: SourceUrl | None = None

    async def fetch(self, url: SourceUrl) -> FetchedPosting:
        self.requested_url = url
        if self.failure is not None:
            raise self.failure
        assert self.posting is not None, "the fake fetcher was not configured with a result"
        return self.posting


def _install_fetcher(
    app: FastAPI,
    *,
    posting: FetchedPosting | None = None,
    failure: JobPostingFetchFailed | None = None,
) -> _FakeJobPostingFetcher:
    fetcher = _FakeJobPostingFetcher(posting=posting, failure=failure)
    app.dependency_overrides[get_job_posting_fetcher] = lambda: fetcher
    return fetcher


def _fetched_posting(
    text: str = _FETCH_FIXTURE_TEXT, title: str | None = _FETCH_FIXTURE_TITLE
) -> FetchedPosting:
    return FetchedPosting(
        text=JobPostingText(text), title=PostingTitle(title) if title is not None else None
    )


# ---------------------------------------------------------------------------------------------
# Direct-to-repository fixture builders — bypass the (currently unimplemented) POST handler so the
# GET-side tests exercise real behaviour instead of inheriting POST's failure. See module docstring.
# ---------------------------------------------------------------------------------------------


async def _count_job_postings(session: AsyncSession) -> int:
    result = await session.execute(sql_text("SELECT count(*) FROM posting_job_posting"))
    return int(result.scalar_one())


async def _mint_guest_session(
    session: AsyncSession, clock: FixedClock, settings: Settings
) -> tuple[MintedGuestToken, GuestSession]:
    """Mint a real `GuestSession` through `StartGuestSession`, the same use case
    `resolve_or_start_guest_session` calls — production code, called directly rather than modified.

    The repository import is deferred to call time, not hoisted to module scope, for the same
    mapper-configuration reason `deps.py::get_base_cv_repository` documents: that module reads
    `GuestSession._id` as a plain attribute at *import* time to build its `InstrumentedAttribute`
    casts, which only exists once `configure_mappings()` has run — and `conftest.py`'s `_mappings`
    fixture runs it well after collection, when this module's own imports would otherwise fire.

    **Commits, rather than flushes, on purpose.** `conftest.py`'s `session` fixture binds through
    `join_transaction_mode="create_savepoint"`, so a `commit()` here only releases a SAVEPOINT —
    isolation from other tests is unaffected. It matters because every request this router currently
    answers ends in an unhandled exception (T25's skeleton), and `_committing_session_override`
    reacts to that by calling `session.rollback()` on this same, shared session — which would erase
    anything seeded here that was only *flushed*, not committed, the moment a second request in the
    same test fails. A committed SAVEPOINT survives that rollback; a merely-flushed row does not.
    """
    from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
        SqlAlchemyGuestSessionRepository,
    )

    sessions = SqlAlchemyGuestSessionRepository(session)
    start = StartGuestSession(sessions, clock, retention_hours=settings.guest_retention_hours)
    minted = mint_guest_token()
    guest_session = await start(minted.token_hash)
    await session.commit()
    return minted, guest_session


async def _seed_posting(
    session: AsyncSession,
    *,
    guest_session_id: GuestSessionId,
    clock: FixedClock,
    text: str = _FETCH_FIXTURE_TEXT,
    title: str | None = None,
    source_url: str | None = None,
) -> JobPosting:
    """Deferred import — see `_mint_guest_session`'s docstring for why. Commits rather than flushes
    for the identical reason given there."""
    from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
        SqlAlchemyJobPostingRepository,
    )

    repo = SqlAlchemyJobPostingRepository(session)
    posting_id = repo.next_identity()
    if source_url is not None:
        posting = JobPosting.from_fetched_url(
            id=posting_id,
            guest_session_id=guest_session_id,
            url=SourceUrl(source_url),
            fetched=FetchedPosting(
                text=JobPostingText(text), title=PostingTitle(title) if title is not None else None
            ),
            created_at=clock.now(),
        )
    else:
        posting = JobPosting.from_pasted_text(
            id=posting_id,
            guest_session_id=guest_session_id,
            text=JobPostingText(text),
            created_at=clock.now(),
        )
    await repo.add(posting)
    await session.commit()
    return posting


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
    """Applies `clear_redis` (conftest.py) to every test in this module. `posting:create` and
    `posting:fetch` are not touched by the per-test transaction rollback (CLAUDE.md)."""
    return None


# ---------------------------------------------------------------------------------------------
# P-1 — body is not JSON, or is empty
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_body", [pytest.param(b"", id="empty"), pytest.param(b"not json at all", id="not-json")]
)
async def test_body_that_is_not_valid_json_returns_422_validation_error(
    client: AsyncClient, raw_body: bytes
) -> None:
    response = await client.post(
        "/api/job-postings", content=raw_body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


# ---------------------------------------------------------------------------------------------
# P-2 — source missing, or not one of pasted|fetched
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"text": "x" * 150}, id="source-missing"),
        pytest.param({"source": "uploaded", "text": "x" * 150}, id="source-unknown"),
    ],
)
async def test_missing_or_unknown_source_returns_422_validation_error(
    client: AsyncClient, body: dict[str, object]
) -> None:
    response = await client.post("/api/job-postings", json=body)

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


# ---------------------------------------------------------------------------------------------
# P-3 — the source-specific required field is missing, on either arm
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"source": "pasted"}, id="pasted-no-text"),
        pytest.param({"source": "fetched"}, id="fetched-no-url"),
    ],
)
async def test_source_specific_field_missing_returns_422_validation_error(
    client: AsyncClient, body: dict[str, object]
) -> None:
    response = await client.post("/api/job-postings", json=body)

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


# ---------------------------------------------------------------------------------------------
# P-7 / OQ-4 — the 256 KiB JSON body cap, refused on Content-Length before parsing
# ---------------------------------------------------------------------------------------------


async def test_json_body_over_the_cap_returns_413_request_too_large(
    client: AsyncClient, settings: Settings
) -> None:
    oversized_text = "a" * (settings.json_request_max_bytes + 1024)
    body = ('{"source":"pasted","text":"' + oversized_text + '"}').encode()
    assert len(body) > settings.json_request_max_bytes

    response = await client.post(
        "/api/job-postings", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 413, response.text
    assert _error_code(response) == "request_too_large"


# ---------------------------------------------------------------------------------------------
# P-4, P-5, P-6 — the pasted-text rules, reached at the DOMAIN, not the schema
# ---------------------------------------------------------------------------------------------


async def test_whitespace_only_pasted_text_returns_422_posting_text_too_short(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/job-postings", json={"source": "pasted", "text": "   \n\t  "}
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "posting_text_too_short"


async def test_pasted_text_with_99_non_whitespace_characters_returns_422_too_short(
    client: AsyncClient,
) -> None:
    response = await client.post("/api/job-postings", json={"source": "pasted", "text": "a" * 99})

    assert response.status_code == 422, response.text
    assert _error_code(response) == "posting_text_too_short"


async def test_pasted_text_over_30000_normalized_characters_reaches_the_domain_not_pydantic(
    client: AsyncClient,
) -> None:
    """The schema's own ceiling (`max_length=40_000`) sits strictly above the domain's 30,000, so a
    30,001-character paste must be refused by `JobPostingText` with its own code and message — not by
    a generic Pydantic validation error that says nothing about the real limit."""
    response = await client.post(
        "/api/job-postings", json={"source": "pasted", "text": "a" * 30_001}
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "posting_text_too_long"


# ---------------------------------------------------------------------------------------------
# P-8, P-9, P-10 — the URL rules
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("file:///etc/passwd", id="file"),
        pytest.param("gopher://example.com/", id="gopher"),
        pytest.param("javascript:alert(1)", id="javascript"),
        pytest.param("data:text/html,hello", id="data"),
    ],
)
async def test_disallowed_url_scheme_returns_422_invalid_source_url(
    client: AsyncClient, url: str
) -> None:
    response = await client.post("/api/job-postings", json={"source": "fetched", "url": url})

    assert response.status_code == 422, response.text
    assert _error_code(response) == "invalid_source_url"


async def test_url_with_userinfo_returns_422_invalid_source_url(client: AsyncClient) -> None:
    response = await client.post(
        "/api/job-postings",
        json={"source": "fetched", "url": "http://user:pass@example.com/jobs/1"},
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "invalid_source_url"


async def test_url_with_no_host_returns_422_invalid_source_url(client: AsyncClient) -> None:
    response = await client.post(
        "/api/job-postings", json={"source": "fetched", "url": "http:///jobs/1"}
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "invalid_source_url"


async def test_url_with_a_control_character_returns_422_invalid_source_url(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/job-postings", json={"source": "fetched", "url": "http://example.com/\x01jobs/1"}
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "invalid_source_url"


async def test_url_over_2048_characters_is_rejected_by_the_schema_before_the_domain(
    client: AsyncClient,
) -> None:
    """P-10 also names a URL over 2,048 characters, but `FetchedJobPostingRequest.url` already caps
    at `max_length=2_048` — the same ceiling `SourceUrl` enforces — so a 2,049-character URL never
    reaches the domain; it is refused by the schema with the generic `validation_error`. This is why
    the failure contract lists P-10 as tested by the domain unit table alone, with no '+ API' the way
    P-8/P-9 have: the domain's own `> 2048` branch is reachable only by constructing `SourceUrl`
    directly, never through this endpoint. Recorded here as the schema-boundary assertion it actually
    is, not mislabelled as `invalid_source_url`.
    """
    response = await client.post(
        "/api/job-postings",
        json={"source": "fetched", "url": "https://example.com/" + "a" * 2040},
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "validation_error"


# ---------------------------------------------------------------------------------------------
# AC-1, AC-2 — the two happy paths
# ---------------------------------------------------------------------------------------------


async def test_pasted_text_is_accepted_and_normalized_201(client: AsyncClient) -> None:
    response = await client.post(
        "/api/job-postings", json={"source": "pasted", "text": PASTED_TEXT_RAW}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source"] == "pasted"
    assert body["source_url"] is None
    assert body["title"] is None
    assert body["character_count"] == _EXPECTED_PASTED_CHARACTER_COUNT
    assert body["text"] == _EXPECTED_PASTED_NORMALIZED_TEXT


async def test_fetched_url_is_accepted_normalized_and_returns_the_extracted_text_201(
    client: AsyncClient, app: FastAPI
) -> None:
    fetcher = _install_fetcher(app, posting=_fetched_posting())

    response = await client.post(
        "/api/job-postings",
        json={"source": "fetched", "url": "HTTPS://JOBS.EXAMPLE.COM/postings/1234#section-2"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source"] == "fetched"
    # Lowercased scheme and host, fragment stripped (AC-2).
    assert body["source_url"] == "https://jobs.example.com/postings/1234"
    assert body["title"] == _FETCH_FIXTURE_TITLE
    assert body["character_count"] == len(_FETCH_FIXTURE_TEXT)
    assert body["text"] == _FETCH_FIXTURE_TEXT
    assert fetcher.requested_url is not None


# ---------------------------------------------------------------------------------------------
# P-11 … P-25 / AC-11 — every fetch failure, and AC-12's no-row guarantee
# ---------------------------------------------------------------------------------------------

_FETCH_FAILURE_CASES: list[tuple[Callable[[], JobPostingFetchFailed], int, str]] = [
    (SourceUrlNotAllowed, 422, "fetch_blocked"),
    (SourceUnreachable, 502, "source_unreachable"),
    (SourceTimedOut, 504, "source_timed_out"),
    (SourceRejectedRequest, 502, "source_rejected"),
    (SourceTooManyRedirects, 502, "source_too_many_redirects"),
    (SourceResponseTooLarge, 502, "source_response_too_large"),
    (SourceNotHtml, 502, "source_not_html"),
    (SourceHasNoReadableText, 502, "source_no_readable_text"),
    (SourceTextTooLong, 502, "source_text_too_long"),
    (lambda: JobPostingFetchFailed(FetchFailureReason.FETCHER_ERROR), 502, "fetcher_error"),
]
_FETCH_FAILURE_IDS = [
    "blocked_target",
    "unreachable",
    "timed_out",
    "rejected",
    "too_many_redirects",
    "response_too_large",
    "not_html",
    "no_readable_text",
    "text_too_long",
    "fetcher_error",
]


@pytest.mark.parametrize(
    ("make_failure", "expected_status", "expected_code"),
    _FETCH_FAILURE_CASES,
    ids=_FETCH_FAILURE_IDS,
)
async def test_fetch_failure_returns_its_documented_status_and_code(
    client: AsyncClient,
    app: FastAPI,
    make_failure: Callable[[], JobPostingFetchFailed],
    expected_status: int,
    expected_code: str,
) -> None:
    _install_fetcher(app, failure=make_failure())

    response = await client.post(
        "/api/job-postings",
        json={"source": "fetched", "url": "https://jobs.example.com/postings/9"},
    )

    assert response.status_code == expected_status, response.text
    assert _error_code(response) == expected_code


@pytest.mark.parametrize(
    ("make_failure", "expected_status", "_expected_code"),
    _FETCH_FAILURE_CASES,
    ids=_FETCH_FAILURE_IDS,
)
async def test_fetch_failure_creates_no_job_posting_row(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    make_failure: Callable[[], JobPostingFetchFailed],
    expected_status: int,
    _expected_code: str,
) -> None:
    """AC-12: a failed fetch is an HTTP error with no artifact behind it — asserted positively, with
    a count taken before and after, never inferred from the response alone."""
    _install_fetcher(app, failure=make_failure())
    before = await _count_job_postings(session)

    response = await client.post(
        "/api/job-postings",
        json={"source": "fetched", "url": "https://jobs.example.com/postings/9"},
    )

    assert response.status_code == expected_status, response.text
    after = await _count_job_postings(session)
    assert after == before, "a failed fetch must not create a posting_job_posting row"


# ---------------------------------------------------------------------------------------------
# AC-13 — every fetch-failure message names the paste fallback
# ---------------------------------------------------------------------------------------------


def test_every_fetch_failure_message_names_the_paste_fallback() -> None:
    """FR-2's product answer to a fetch that fails is a message *and* an action; this is the
    machine-checkable half of that at the API layer — the server-owned mapping
    (`infrastructure/api/errors.py::_fetch_failure_to_http`) must never hand back a sentence that
    reads as "try again" without a path forward."""
    reasons = list(FetchFailureReason)
    assert reasons, "expected at least one FetchFailureReason to check"

    for reason in reasons:
        exc = _fetch_failure_to_http(reason)
        # `cast`, not an unjustified `Any` — see `main.py::handle_http_exception`'s identical cast
        # for why: Starlette types `HTTPException.detail` as `str | None`, so mypy sees that and
        # nothing else, even though every raise site in this codebase (`errors.py` included) always
        # passes a `dict`.
        detail = cast("object", exc.detail)
        assert isinstance(detail, dict)
        message = str(detail["message"])
        assert "paste" in message.lower(), f"{reason}: {message!r} does not mention pasting"


# ---------------------------------------------------------------------------------------------
# P-27, P-28 — the guest cookie on POST: missing or unknown both mint a fresh session
# ---------------------------------------------------------------------------------------------


async def test_post_with_no_cookie_mints_a_new_session(client: AsyncClient) -> None:
    response = await client.post(
        "/api/job-postings", json={"source": "pasted", "text": PASTED_TEXT_RAW}
    )

    assert response.status_code == 201, response.text
    assert _guest_cookie_header(response) is not None


async def test_post_with_unknown_cookie_mints_a_new_session(client: AsyncClient) -> None:
    client.cookies.set(COOKIE_NAME, "a-token-that-was-never-minted-by-this-server")

    response = await client.post(
        "/api/job-postings", json={"source": "pasted", "text": PASTED_TEXT_RAW}
    )

    assert response.status_code == 201, response.text
    assert _guest_cookie_header(response) is not None


async def test_expired_cookie_on_post_mints_a_fresh_session(
    client: AsyncClient, app: FastAPI
) -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    app.dependency_overrides[get_clock] = lambda: clock

    minted = await client.post(
        "/api/job-postings", json={"source": "pasted", "text": PASTED_TEXT_RAW}
    )
    assert minted.status_code == 201, minted.text
    first_token = _guest_cookie_value(minted)

    clock.advance(25 * 3600)  # past the default 24h retention window

    renewed = await client.post(
        "/api/job-postings", json={"source": "pasted", "text": PASTED_TEXT_RAW}
    )
    assert renewed.status_code == 201, renewed.text
    second_token = _guest_cookie_value(renewed)
    assert second_token is not None
    assert second_token != first_token, "an expired session must be replaced, not renewed in place"


# ---------------------------------------------------------------------------------------------
# P-29 — the guest cookie on GET: missing, unknown, or expired all refuse with 401
# ---------------------------------------------------------------------------------------------


async def test_get_list_with_no_cookie_returns_401(client: AsyncClient) -> None:
    response = await client.get("/api/job-postings")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


async def test_get_one_with_no_cookie_returns_401(client: AsyncClient) -> None:
    response = await client.get(f"/api/job-postings/{uuid4()}")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


async def test_get_list_with_unknown_cookie_returns_401(client: AsyncClient) -> None:
    client.cookies.set(COOKIE_NAME, "a-token-that-was-never-minted-by-this-server")

    response = await client.get("/api/job-postings")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


async def test_get_one_with_expired_cookie_returns_401(
    client: AsyncClient, app: FastAPI, session: AsyncSession, settings: Settings
) -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    app.dependency_overrides[get_clock] = lambda: clock
    minted, _guest = await _mint_guest_session(session, clock, settings)
    client.cookies.set(COOKIE_NAME, minted.token)

    clock.advance(25 * 3600)

    response = await client.get(f"/api/job-postings/{uuid4()}")

    assert response.status_code == 401, response.text
    assert _error_code(response) == "guest_session_expired"


# ---------------------------------------------------------------------------------------------
# P-30, P-31, AC-14 — the link, not the id, authorizes a read; a malformed id is FastAPI's own 422
# ---------------------------------------------------------------------------------------------


async def test_reading_own_job_posting_returns_the_full_text(
    client: AsyncClient, app: FastAPI, session: AsyncSession, clock: FixedClock, settings: Settings
) -> None:
    app.dependency_overrides[get_clock] = lambda: clock
    minted, guest = await _mint_guest_session(session, clock, settings)
    posting = await _seed_posting(session, guest_session_id=guest.id, clock=clock)
    client.cookies.set(COOKIE_NAME, minted.token)

    response = await client.get(f"/api/job-postings/{posting.id.value}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(posting.id.value)
    assert body["source"] == "pasted"
    assert body["text"] == _FETCH_FIXTURE_TEXT


async def test_reading_another_sessions_job_posting_returns_404(
    client: AsyncClient, app: FastAPI, session: AsyncSession, clock: FixedClock, settings: Settings
) -> None:
    app.dependency_overrides[get_clock] = lambda: clock
    _owner_token, owner = await _mint_guest_session(session, clock, settings)
    reader_token, _reader = await _mint_guest_session(session, clock, settings)
    posting = await _seed_posting(session, guest_session_id=owner.id, clock=clock)
    client.cookies.set(COOKIE_NAME, reader_token.token)

    response = await client.get(f"/api/job-postings/{posting.id.value}")

    assert response.status_code == 404, response.text
    assert _error_code(response) == "job_posting_not_found"


async def test_not_mine_and_nonexistent_return_byte_identical_404(
    client: AsyncClient, app: FastAPI, session: AsyncSession, clock: FixedClock, settings: Settings
) -> None:
    """AC-14: "404, identical to a nonexistent id. 403 would confirm the id exists." — asserts the
    two responses carry the same machine-readable code, not merely that both happen to be 404."""
    app.dependency_overrides[get_clock] = lambda: clock
    _owner_token, owner = await _mint_guest_session(session, clock, settings)
    reader_token, _reader = await _mint_guest_session(session, clock, settings)
    posting = await _seed_posting(session, guest_session_id=owner.id, clock=clock)
    client.cookies.set(COOKIE_NAME, reader_token.token)

    not_mine = await client.get(f"/api/job-postings/{posting.id.value}")
    never_existed = await client.get(f"/api/job-postings/{uuid4()}")

    assert not_mine.status_code == never_existed.status_code == 404
    assert _error_code(not_mine) == _error_code(never_existed) == "job_posting_not_found"


async def test_malformed_uuid_in_path_returns_422(
    client: AsyncClient, app: FastAPI, session: AsyncSession, clock: FixedClock, settings: Settings
) -> None:
    app.dependency_overrides[get_clock] = lambda: clock
    minted, _guest = await _mint_guest_session(session, clock, settings)
    client.cookies.set(COOKIE_NAME, minted.token)

    response = await client.get("/api/job-postings/not-a-uuid")

    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------------------------
# The GETs: an empty list is 200, never 404; the list carries a preview, never the full text;
# both GETs answer Cache-Control: no-store.
# ---------------------------------------------------------------------------------------------


async def test_get_list_for_a_session_with_no_postings_returns_an_empty_list_not_404(
    client: AsyncClient, app: FastAPI, session: AsyncSession, clock: FixedClock, settings: Settings
) -> None:
    app.dependency_overrides[get_clock] = lambda: clock
    minted, _guest = await _mint_guest_session(session, clock, settings)
    client.cookies.set(COOKIE_NAME, minted.token)

    response = await client.get("/api/job-postings")

    assert response.status_code == 200, response.text
    assert response.json() == {"items": []}


async def test_get_list_returns_a_preview_never_the_full_text(
    client: AsyncClient, app: FastAPI, session: AsyncSession, clock: FixedClock, settings: Settings
) -> None:
    long_text = "word " * 400  # far over the 280-character preview cap once normalized
    app.dependency_overrides[get_clock] = lambda: clock
    minted, guest = await _mint_guest_session(session, clock, settings)
    await _seed_posting(session, guest_session_id=guest.id, clock=clock, text=long_text)
    client.cookies.set(COOKIE_NAME, minted.token)

    response = await client.get("/api/job-postings")

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert "text" not in item, "the list must never carry a posting's full text"
    expected_preview = " ".join(long_text.split())[:280]
    assert item["preview"] == expected_preview
    assert len(item["preview"]) <= 280


async def test_both_gets_answer_cache_control_no_store(
    client: AsyncClient, app: FastAPI, session: AsyncSession, clock: FixedClock, settings: Settings
) -> None:
    app.dependency_overrides[get_clock] = lambda: clock
    minted, guest = await _mint_guest_session(session, clock, settings)
    posting = await _seed_posting(session, guest_session_id=guest.id, clock=clock)
    client.cookies.set(COOKIE_NAME, minted.token)

    list_response = await client.get("/api/job-postings")
    detail_response = await client.get(f"/api/job-postings/{posting.id.value}")

    assert list_response.status_code == 200, list_response.text
    assert detail_response.status_code == 200, detail_response.text
    assert list_response.headers.get("cache-control") == "no-store"
    assert detail_response.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------------------------
# P-32 — the per-session cap
# ---------------------------------------------------------------------------------------------


async def test_an_eleventh_job_posting_for_one_session_returns_409(
    client: AsyncClient, settings: Settings
) -> None:
    responses = [
        await client.post("/api/job-postings", json={"source": "pasted", "text": PASTED_TEXT_RAW})
        for _ in range(settings.max_job_postings_per_session + 1)
    ]

    for response in responses[: settings.max_job_postings_per_session]:
        assert response.status_code == 201, response.text

    last = responses[-1]
    assert last.status_code == 409, last.text
    assert _error_code(last) == "too_many_job_postings"


# ---------------------------------------------------------------------------------------------
# P-33 — rate limiting: 20 creates/h/session, 10 fetches/h/session, 30 fetches/h/IP
# ---------------------------------------------------------------------------------------------


async def test_more_than_the_per_session_create_limit_returns_429(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    _override_settings(app, settings, posting_rate_limit_per_hour=2)

    first = await client.post(
        "/api/job-postings", json={"source": "pasted", "text": PASTED_TEXT_RAW}
    )
    second = await client.post(
        "/api/job-postings", json={"source": "pasted", "text": PASTED_TEXT_RAW}
    )
    third = await client.post(
        "/api/job-postings", json={"source": "pasted", "text": PASTED_TEXT_RAW}
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert third.status_code == 429, third.text
    assert _error_code(third) == "rate_limited"
    assert "retry-after" in {name.lower() for name in third.headers}


async def test_more_than_the_per_session_fetch_limit_returns_429(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    _override_settings(app, settings, posting_fetch_rate_limit_per_hour=2)
    _install_fetcher(app, posting=_fetched_posting())
    fetch_body = {"source": "fetched", "url": "https://jobs.example.com/postings/1"}

    first = await client.post("/api/job-postings", json=fetch_body)
    second = await client.post("/api/job-postings", json=fetch_body)
    third = await client.post("/api/job-postings", json=fetch_body)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert third.status_code == 429, third.text
    assert _error_code(third) == "rate_limited"
    assert "retry-after" in {name.lower() for name in third.headers}


async def test_more_than_the_per_ip_fetch_limit_returns_429(
    app: FastAPI, settings: Settings
) -> None:
    """Each request mints its *own* fresh guest session (a new client, no cookie), so only the
    shared, fixed client IP (`ASGITransport`'s default peer) accumulates across them — isolating the
    per-IP limit from the per-session one, exactly as `test_intake.py`'s twin does."""
    _override_settings(app, settings, posting_fetch_rate_limit_per_ip_per_hour=2)
    _install_fetcher(app, posting=_fetched_posting())
    fetch_body = {"source": "fetched", "url": "https://jobs.example.com/postings/1"}

    async def fetch_from_a_fresh_session() -> Response:
        async with _new_client(app) as one_shot_client:
            return await one_shot_client.post("/api/job-postings", json=fetch_body)

    first = await fetch_from_a_fresh_session()
    second = await fetch_from_a_fresh_session()
    third = await fetch_from_a_fresh_session()

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert third.status_code == 429, third.text
    assert _error_code(third) == "rate_limited"


# ---------------------------------------------------------------------------------------------
# P-34, P-35 / AC-17 — Redis unreachable: create fails OPEN, fetch fails CLOSED
# ---------------------------------------------------------------------------------------------


async def test_create_limiter_fails_open_when_redis_is_unreachable(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    _override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await client.post(
        "/api/job-postings", json={"source": "pasted", "text": PASTED_TEXT_RAW}
    )

    assert response.status_code == 201, response.text


async def test_fetch_limiter_fails_closed_when_redis_is_unreachable(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    _override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")
    _install_fetcher(app, posting=_fetched_posting())

    response = await client.post(
        "/api/job-postings",
        json={"source": "fetched", "url": "https://jobs.example.com/postings/1"},
    )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "rate_limit_unavailable"


# ---------------------------------------------------------------------------------------------
# P-36 — the commit fails after the aggregate is built
# ---------------------------------------------------------------------------------------------


async def test_commit_failure_returns_503_service_unavailable(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _raise_sqlalchemy_error() -> None:
        raise SQLAlchemyError("simulated commit failure (P-36)")

    monkeypatch.setattr(session, "commit", _raise_sqlalchemy_error)

    response = await client.post(
        "/api/job-postings", json={"source": "pasted", "text": PASTED_TEXT_RAW}
    )

    assert response.status_code == 503, response.text
    assert _error_code(response) == "service_unavailable"


# ---------------------------------------------------------------------------------------------
# P-37 — double submission is not deduplicated
# ---------------------------------------------------------------------------------------------


async def test_submitting_the_same_text_twice_creates_two_distinct_postings(
    client: AsyncClient,
) -> None:
    first = await client.post(
        "/api/job-postings", json={"source": "pasted", "text": PASTED_TEXT_RAW}
    )
    second = await client.post(
        "/api/job-postings", json={"source": "pasted", "text": PASTED_TEXT_RAW}
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]


# ---------------------------------------------------------------------------------------------
# AC-18 — the privacy test: nothing in a full fetch logs the text, the URL's path/query, or an IP
# paired with a host
# ---------------------------------------------------------------------------------------------


async def test_a_full_fetch_never_logs_the_text_or_the_urls_path_or_query(
    client: AsyncClient, app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    _install_fetcher(
        app, posting=_fetched_posting(text=_PRIVACY_FIXTURE_TEXT, title=_FETCH_FIXTURE_TITLE)
    )

    with caplog.at_level(logging.INFO):
        response = await client.post(
            "/api/job-postings",
            json={"source": "fetched", "url": _PRIVACY_FIXTURE_URL},
            headers={"x-forwarded-for": _PRIVACY_FIXTURE_IP},
        )

    assert response.status_code == 201, response.text

    # A capture that caught nothing at all proves nothing — the guard against this test passing for
    # the wrong reason (see test_intake.py's identical AC-12 guard).
    assert caplog.records, "expected the fetch to have produced at least one log record"

    log_output = caplog.text
    assert _PRIVACY_FIXTURE_TEXT_MARKER not in log_output
    assert "secret-path-abc123" not in log_output
    assert "ref=xyz789" not in log_output
    assert _PRIVACY_FIXTURE_IP not in log_output, (
        "the client IP must never reach a log line (Constitution §8) — not even paired with a host"
    )
