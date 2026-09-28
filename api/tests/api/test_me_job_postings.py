"""API tests for `/api/me/job-postings` (slice 2.3, T21 RED; AC-27, AC-28, H-2, H-10, H-11).

Both handlers are T20 skeletons (`NotImplementedError`), so every test that reaches a handler is red
on its status assertion (`assert 500 == 201`), never on an import. Written from the feature spec and
technical plan §4, not from the handlers.

- AC-27: paste and fetch answer **201** `JobPostingResponse` with `expires_at: null` and a
  user-owned row; 1.2's boundary codes and every fetch-failure code (H-2) unchanged, with no row
  behind a failure (ADR-0013); the create limiter is keyed on the **user** (fail open, 1.2's
  choice), the fetch limiter on the user **and** the IP (fail closed, 1.2's choice); 409
  `too_many_job_postings` at the user cap, with **no fetch attempted** (H-10).
- AC-28: `GET ?limit=1..20` lists the user's own postings newest first as 1.2's
  `JobPostingSummary` (a preview, never the text). **The default is 1** — AC-28 says so; the T20
  skeleton's router declares 20. The spec is what this test encodes (reported to the coordinator).

The fetcher is a fake installed on `get_job_posting_fetcher`; no test makes an outbound request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

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
from tailorcraft.domain.posting.value_objects import (
    FetchedPosting,
    FetchFailureReason,
    JobPostingText,
    PostingTitle,
    SourceUrl,
)
from tailorcraft.infrastructure.api.deps import get_job_posting_fetcher
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    JOB_POSTING_RESPONSE_KEYS,
    JOB_POSTING_SUMMARY_KEYS,
    ME_POSTINGS,
    count_rows,
    error_body,
    error_code,
    mint_guest,
    new_client,
    override_settings,
    register,
)
from tests.integration.owners import pasted_posting

_PASTE = {"source": "pasted", "text": "We are hiring a platform engineer. " * 8}
_FETCH = {"source": "fetched", "url": "https://jobs.example.com/postings/1234"}


@dataclass
class _FakeFetcher:
    posting: FetchedPosting | None = None
    failure: JobPostingFetchFailed | None = None
    calls: int = 0

    async def fetch(self, url: SourceUrl) -> FetchedPosting:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        assert self.posting is not None
        return self.posting


def _install_fetcher(app: FastAPI, failure: JobPostingFetchFailed | None = None) -> _FakeFetcher:
    fetcher = _FakeFetcher(
        posting=FetchedPosting(
            text=JobPostingText("Senior platform engineer, remote. " * 8),
            title=PostingTitle("Senior Platform Engineer"),
        ),
        failure=failure,
    )
    app.dependency_overrides[get_job_posting_fetcher] = lambda: fetcher
    return fetcher


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """The limiters' counters live in Redis, which the rollback never reaches."""


# --- AC-27: create ------------------------------------------------------------------------------


async def test_a_pasted_posting_is_201_user_owned_with_null_expires_at(
    client: AsyncClient, settings: Settings, session: AsyncSession
) -> None:
    account = await register(client, settings)

    response = await client.post(ME_POSTINGS, json=_PASTE, headers=account.headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == JOB_POSTING_RESPONSE_KEYS
    assert body["expires_at"] is None
    assert body["source"] == "pasted"
    assert response.headers.get("cache-control") == "no-store"
    assert (
        await count_rows(
            session, "posting_job_posting", id=UUID(body["id"]), user_id=account.user_id.value
        )
        == 1
    )


async def test_a_fetched_posting_is_201_user_owned(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession
) -> None:
    fetcher = _install_fetcher(app)
    account = await register(client, settings)

    response = await client.post(ME_POSTINGS, json=_FETCH, headers=account.headers)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["expires_at"] is None
    assert body["source"] == "fetched"
    assert body["title"] == "Senior Platform Engineer"
    assert fetcher.calls == 1
    assert (
        await count_rows(
            session, "posting_job_posting", id=UUID(body["id"]), user_id=account.user_id.value
        )
        == 1
    )


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        pytest.param({"source": "pasted", "text": "   "}, "posting_text_too_short", id="too-short"),
        pytest.param(
            {"source": "pasted", "text": "x" * 30_001}, "posting_text_too_long", id="too-long"
        ),
        pytest.param(
            {"source": "fetched", "url": "file:///etc/passwd"}, "invalid_source_url", id="scheme"
        ),
        pytest.param({"source": "uploaded", "text": "x" * 200}, "validation_error", id="source"),
    ],
)
async def test_1_2s_boundary_codes_are_unchanged(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    payload: dict[str, str],
    code: str,
) -> None:
    account = await register(client, settings)

    response = await client.post(ME_POSTINGS, json=payload, headers=account.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == code
    assert await count_rows(session, "posting_job_posting", user_id=account.user_id.value) == 0


_FETCH_FAILURES: list[tuple[Callable[[], JobPostingFetchFailed], int, str]] = [
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


@pytest.mark.parametrize(
    ("make_failure", "status", "code"),
    _FETCH_FAILURES,
    ids=[code for _, _, code in _FETCH_FAILURES],
)
async def test_h2_every_fetch_failure_keeps_its_code_and_stores_no_row(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    session: AsyncSession,
    make_failure: Callable[[], JobPostingFetchFailed],
    status: int,
    code: str,
) -> None:
    fetcher = _install_fetcher(app, failure=make_failure())
    account = await register(client, settings)

    response = await client.post(ME_POSTINGS, json=_FETCH, headers=account.headers)

    assert response.status_code == status, response.text
    assert error_code(response) == code
    assert fetcher.calls == 1
    assert await count_rows(session, "posting_job_posting", user_id=account.user_id.value) == 0


# --- H-10: the user cap -------------------------------------------------------------------------


async def _fill(session: AsyncSession, owner: object, count: int, clock: FixedClock) -> None:
    repo = SqlAlchemyJobPostingRepository(session)
    for _ in range(count):
        await repo.add(pasted_posting(owner, clock.now()))  # type: ignore[arg-type]
    await session.flush()


async def test_h10_at_the_user_cap_a_fetch_is_409_with_no_fetch_and_no_row(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    override_settings(app, settings, max_job_postings_per_user=2)
    fetcher = _install_fetcher(app)
    account = await register(client, settings)
    await _fill(session, account.owner, 2, clock)

    response = await client.post(ME_POSTINGS, json=_FETCH, headers=account.headers)

    assert response.status_code == 409, response.text
    assert error_code(response) == "too_many_job_postings"
    assert "2" in str(error_body(response)["message"]), "the message names the cap"
    assert fetcher.calls == 0
    assert await count_rows(session, "posting_job_posting", user_id=account.user_id.value) == 2


async def test_h10_below_the_user_cap_the_posting_is_stored(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    override_settings(app, settings, max_job_postings_per_user=2)
    account = await register(client, settings)
    await _fill(session, account.owner, 1, clock)

    response = await client.post(ME_POSTINGS, json=_PASTE, headers=account.headers)

    assert response.status_code == 201, response.text
    assert await count_rows(session, "posting_job_posting", user_id=account.user_id.value) == 2


# --- H-11: the limiters, keyed on the user (and the IP for fetch) --------------------------------


async def test_h11_the_create_limiter_is_per_user(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    """A second paste by the same user is 429; another user's first paste is not — the bucket is
    the user's, not the IP's or a global one."""
    override_settings(app, settings, posting_rate_limit_per_hour=1)
    alice = await register(client, settings)
    bob = await register(client, settings)

    first = await client.post(ME_POSTINGS, json=_PASTE, headers=alice.headers)
    second = await client.post(ME_POSTINGS, json=_PASTE, headers=alice.headers)
    other = await client.post(ME_POSTINGS, json=_PASTE, headers=bob.headers)

    assert first.status_code == 201, first.text
    assert second.status_code == 429, second.text
    assert error_code(second) == "rate_limited"
    assert "retry-after" in {name.lower() for name in second.headers}
    assert other.status_code == 201, other.text


async def test_h11_the_fetch_limiter_is_also_per_ip(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    override_settings(
        app,
        settings,
        posting_fetch_rate_limit_per_hour=100,
        posting_fetch_rate_limit_per_ip_per_hour=1,
    )
    _install_fetcher(app)
    alice = await register(client, settings)
    bob = await register(client, settings)

    first = await client.post(ME_POSTINGS, json=_FETCH, headers=alice.headers)
    second = await client.post(ME_POSTINGS, json=_FETCH, headers=bob.headers)

    assert first.status_code == 201, first.text
    assert second.status_code == 429, second.text
    assert error_code(second) == "rate_limited"


async def test_h11_redis_down_paste_fails_open(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    account = await register(client, settings)
    override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await client.post(ME_POSTINGS, json=_PASTE, headers=account.headers)

    assert response.status_code == 201, response.text


async def test_h11_redis_down_fetch_fails_closed_with_no_fetch(
    client: AsyncClient, app: FastAPI, settings: Settings, session: AsyncSession
) -> None:
    fetcher = _install_fetcher(app)
    account = await register(client, settings)
    override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await client.post(ME_POSTINGS, json=_FETCH, headers=account.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "rate_limit_unavailable"
    assert fetcher.calls == 0
    assert await count_rows(session, "posting_job_posting", user_id=account.user_id.value) == 0


# --- AC-28: the recent list ---------------------------------------------------------------------


async def test_the_recent_list_is_the_users_own_newest_first_as_summaries(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    account = await register(client, settings)
    other = await register(client, settings)
    guest = await mint_guest(client, session)
    repo = SqlAlchemyJobPostingRepository(session)
    mine = [pasted_posting(account.owner, clock.now() - timedelta(minutes=m)) for m in (30, 10, 20)]
    for posting in [
        *mine,
        pasted_posting(other.owner, clock.now()),
        pasted_posting(guest, clock.now()),
    ]:
        await repo.add(posting)
    await session.flush()

    response = await client.get(f"{ME_POSTINGS}?limit=20", headers=account.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items"}
    newest_first = sorted(mine, key=lambda p: p.created_at, reverse=True)
    assert [item["id"] for item in body["items"]] == [str(p.id.value) for p in newest_first]
    for item in body["items"]:
        assert set(item) == JOB_POSTING_SUMMARY_KEYS
        assert item["expires_at"] is None
        assert "text" not in item
    assert response.headers.get("cache-control") == "no-store"


async def test_the_recent_list_defaults_to_one(
    client: AsyncClient, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    """AC-28: `limit=1..20 (default 1)` — the workspace shows the newest posting."""
    account = await register(client, settings)
    repo = SqlAlchemyJobPostingRepository(session)
    newest = pasted_posting(account.owner, clock.now())
    await repo.add(pasted_posting(account.owner, clock.now() - timedelta(minutes=5)))
    await repo.add(newest)
    await session.flush()

    response = await client.get(ME_POSTINGS, headers=account.headers)

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [str(newest.id.value)]


@pytest.mark.parametrize("limit", ["0", "21", "x"])
async def test_the_recent_list_refuses_a_limit_outside_1_to_20(
    client: AsyncClient, settings: Settings, limit: str
) -> None:
    account = await register(client, settings)

    response = await client.get(f"{ME_POSTINGS}?limit={limit}", headers=account.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"


async def test_the_recent_list_accepts_limit_twenty(
    client: AsyncClient, settings: Settings
) -> None:
    """The discriminating positive for the bounds test above: 20 is inside."""
    account = await register(client, settings)

    response = await client.get(f"{ME_POSTINGS}?limit=20", headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"items": []}
