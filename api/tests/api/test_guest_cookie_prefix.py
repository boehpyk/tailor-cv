"""The guest cookie is `__Host-tc_guest` (slice identity-host-prefixed-guest-cookie, T4; AC-1, AC-2,
AC-4, AC-7, AC-8, G-2, G-4, G-5, G-6). Written red-first from the spec, against the unchanged
`COOKIE_NAME = "tc_guest"`.

**Traps this file is built around** (task list, "Traps"):

- **The names are string literals.** A test that reads `COOKIE_NAME` cannot fail on a rename, so
  `"__Host-tc_guest"` and the legacy `"tc_guest"` are spelled out here and the constant is never
  imported.
- **A legacy cookie is planted, never inherited from a previous response**: `client.cookies.set(
  "tc_guest", token)` after seeding a live session whose hash this test knows. The session is
  committed (a flush-only seed dies with a refused request, 2.3).
- **Absence needs a positive control.** "No `Set-Cookie`" is satisfied by a handler that does
  nothing, so every absence sits beside a discriminating positive: the legacy row is live before the
  request, a new session id differs from it, a 200 precedes the "no cookie".
- **Time.** Sessions are started with the system clock (the routes judge expiry by it); the fixture
  `FixedClock` is in the past and would mint an already-expired session.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.identity.start_guest_session import StartGuestSession
from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.infrastructure.api.guest_session import mint_guest_token
from tailorcraft.infrastructure.clock import FixedClock, SystemClock
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    SAMPLE_CV,
    Entry,
    count_rows,
    error_code,
    new_client,
    override_settings,
    register,
    seed_entry,
)

PREFIXED = "__Host-tc_guest"
LEGACY = "tc_guest"
CLAIM_URL = "/api/me/guest-work/claim"
POSTING_BODY = {"source": "pasted", "text": "Senior engineer wanted. " * 10}
ZEROS = {
    "base_cvs": 0,
    "job_postings": 0,
    "tailoring_runs": 0,
    "export_jobs": 0,
    "working_copies_dropped": 0,
}


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """The mint and claim limiters live in Redis; the rollback never reaches it."""


# --- helpers ------------------------------------------------------------------------------------


def _guest_cookies(response: Response) -> list[dict[str, str]]:
    """Every `Set-Cookie` whose name is either guest-cookie spelling, parsed. The attribute keys are
    lower-cased; a flag attribute (`Secure`) maps to ''."""
    parsed: list[dict[str, str]] = []
    for header in response.headers.get_list("set-cookie"):
        first, *attributes = [part.strip() for part in header.split(";")]
        name, _, value = first.partition("=")
        if name not in (PREFIXED, LEGACY):
            continue
        entry = {"name": name, "value": value.strip('"')}
        for attribute in attributes:
            key, _, attribute_value = attribute.partition("=")
            entry[key.lower()] = attribute_value
        parsed.append(entry)
    return parsed


async def _live_guest(
    session: AsyncSession, settings: Settings, clock: FixedClock
) -> tuple[str, GuestOwner, Entry]:
    """A committed, live guest session with a known token and one seeded entry (a CV, a posting, a
    run, an export). Returns the raw token, the owner and the entry."""
    repository = SqlAlchemyGuestSessionRepository(session)
    start = StartGuestSession(
        repository, SystemClock(), retention_hours=settings.guest_retention_hours
    )
    minted = mint_guest_token()
    guest_session = await start(minted.token_hash)
    await session.commit()
    owner = GuestOwner(guest_session.id)
    entry = await seed_entry(session, settings, owner, at=clock.now() - timedelta(minutes=10))
    return minted.token, owner, entry


async def _rows(session: AsyncSession, owner: GuestOwner) -> dict[str, int]:
    sid = owner.guest_session_id.value
    return {
        table: await count_rows(session, table, guest_session_id=sid)
        for table in ("intake_base_cv", "posting_job_posting", "tailoring_run", "export_job")
    }


async def _session_exists(session: AsyncSession, owner: GuestOwner) -> int:
    return await count_rows(session, "identity_guest_session", id=owner.guest_session_id.value)


async def _owning_session(session: AsyncSession, cv_id: str) -> UUID:
    result = await session.execute(
        text("SELECT guest_session_id FROM intake_base_cv WHERE id = :id"), {"id": UUID(cv_id)}
    )
    return UUID(str(result.scalar_one()))


async def _upload(client: AsyncClient) -> Response:
    return await client.post(
        "/api/base-cvs", files={"file": ("sample.txt", SAMPLE_CV, "text/plain")}
    )


# --- AC-1: the mint sets exactly one __Host- cookie, in every environment -----------------------


@pytest.mark.parametrize("app_env", ["test", "dev", "production"])
@pytest.mark.parametrize("route", ["/api/base-cvs", "/api/job-postings"])
async def test_ac1_a_minting_post_sets_one_prefixed_secure_cookie_in_every_environment(
    app: FastAPI, settings: Settings, client: AsyncClient, app_env: str, route: str
) -> None:
    modified = override_settings(app, settings, app_env=app_env)

    if route == "/api/base-cvs":
        response = await _upload(client)
    else:
        response = await client.post(route, json=POSTING_BODY)

    assert response.status_code == 201, response.text
    cookies = _guest_cookies(response)
    assert [c["name"] for c in cookies] == [PREFIXED], cookies
    cookie = cookies[0]
    assert cookie["value"] != ""
    assert "secure" in cookie, cookie
    assert "httponly" in cookie, cookie
    assert cookie.get("path") == "/", cookie
    assert cookie.get("samesite", "").lower() == "lax", cookie
    assert cookie.get("max-age") == str(modified.guest_retention_hours * 3600), cookie
    assert "domain" not in cookie, cookie


# --- AC-2: nothing sets the legacy name ----------------------------------------------------------


async def test_ac2_the_mint_and_the_claim_never_set_a_cookie_named_tc_guest(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    mint = await _upload(client)
    assert mint.status_code == 201, mint.text
    # Positive control: the mint did set a guest cookie, under the new name.
    assert [c["name"] for c in _guest_cookies(mint)] == [PREFIXED]

    account = await register(client, settings)
    token, _, _ = await _live_guest(session, settings, clock)
    client.cookies.clear()
    client.cookies.set(PREFIXED, token)
    claim = await client.post(CLAIM_URL, headers=account.headers)

    assert claim.status_code == 200, claim.text
    names = [c["name"] for c in _guest_cookies(claim)]
    # Positive control: the claim did clear a guest cookie, under the new name.
    assert names == [PREFIXED]
    assert LEGACY not in names


# --- AC-4: the claim's clear carries the set's attributes, Secure included -----------------------


async def test_ac4_the_claim_clears_the_prefixed_cookie_with_secure_and_no_domain(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    account = await register(client, settings)
    token, owner, _ = await _live_guest(session, settings, clock)
    client.cookies.set(PREFIXED, token)

    response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    cookies = _guest_cookies(response)
    assert len(cookies) == 1, cookies
    cookie = cookies[0]
    assert cookie["name"] == PREFIXED
    assert cookie["value"] == ""
    assert cookie.get("max-age") == "0"
    assert cookie.get("path") == "/"
    assert "secure" in cookie, "a __Host- clear without Secure is silently ignored by a browser"
    assert "httponly" in cookie
    assert cookie.get("samesite", "").lower() == "lax"
    assert "domain" not in cookie
    # And the claim really consumed the session named by that cookie.
    assert await _session_exists(session, owner) == 0


# --- AC-7 / G-2 / G-6: a legacy cookie alone is a stranger ---------------------------------------


async def test_ac7_a_legacy_only_get_is_401_guest_session_expired(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    token, owner, _ = await _live_guest(session, settings, clock)
    assert await _session_exists(session, owner) == 1  # positive control: the session is live
    client.cookies.set(LEGACY, token)

    response = await client.get("/api/base-cvs")

    assert response.status_code == 401, response.text
    assert error_code(response) == "guest_session_expired"


async def test_ac7_a_legacy_only_post_mints_a_new_session_and_leaves_the_legacy_one_alone(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    token, owner, _ = await _live_guest(session, settings, clock)
    before = await _rows(session, owner)
    assert before["intake_base_cv"] == 1  # positive control: the legacy session holds a CV
    client.cookies.set(LEGACY, token)

    response = await _upload(client)

    assert response.status_code == 201, response.text
    cookies = _guest_cookies(response)
    assert [c["name"] for c in cookies] == [PREFIXED], cookies
    assert cookies[0]["value"] not in ("", token)
    assert await _owning_session(session, response.json()["id"]) != owner.guest_session_id.value
    assert await _rows(session, owner) == before
    assert await _session_exists(session, owner) == 1


async def test_g6_a_legacy_only_claim_is_zeros_with_no_set_cookie_and_the_legacy_row_stays(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    account = await register(client, settings)
    token, owner, _ = await _live_guest(session, settings, clock)
    before = await _rows(session, owner)
    assert before["tailoring_run"] == 1  # positive control: there was work to (not) claim
    client.cookies.set(LEGACY, token)

    response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json() == ZEROS
    assert response.headers.get_list("set-cookie") == []
    assert await _session_exists(session, owner) == 1
    assert await _rows(session, owner) == before


# --- AC-8 / G-5: both present, disagreeing -------------------------------------------------------


async def _two_guests(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> tuple[GuestOwner, Entry, GuestOwner, Entry]:
    """A under `__Host-tc_guest`, B under the legacy `tc_guest`, both live and holding work."""
    token_a, owner_a, entry_a = await _live_guest(session, settings, clock)
    token_b, owner_b, entry_b = await _live_guest(session, settings, clock)
    assert owner_a != owner_b
    client.cookies.set(PREFIXED, token_a)
    client.cookies.set(LEGACY, token_b)
    return owner_a, entry_a, owner_b, entry_b


async def test_ac8_with_both_cookies_get_lists_only_the_prefixed_sessions_items(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    _, entry_a, _, entry_b = await _two_guests(client, session, settings, clock)

    response = await client.get("/api/base-cvs")

    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["items"]}
    assert str(entry_a.cv_id.value) in ids
    assert str(entry_b.cv_id.value) not in ids


async def test_ac8_with_both_cookies_a_post_lands_in_the_prefixed_session_and_sets_no_cookie(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    owner_a, _, owner_b, _ = await _two_guests(client, session, settings, clock)
    before_a, before_b = await _rows(session, owner_a), await _rows(session, owner_b)

    response = await _upload(client)

    assert response.status_code == 201, response.text
    assert await _owning_session(session, response.json()["id"]) == owner_a.guest_session_id.value
    assert response.headers.get_list("set-cookie") == []
    after_a = await _rows(session, owner_a)
    assert after_a["intake_base_cv"] == before_a["intake_base_cv"] + 1
    assert await _rows(session, owner_b) == before_b


async def test_ac8_with_both_cookies_the_claim_moves_a_clears_the_prefix_and_leaves_b(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    account = await register(client, settings)
    owner_a, _, owner_b, _ = await _two_guests(client, session, settings, clock)
    before_a, before_b = await _rows(session, owner_a), await _rows(session, owner_b)
    assert before_a["tailoring_run"] == 1
    assert before_b["tailoring_run"] == 1

    response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json()["tailoring_runs"] == 1
    cookies = _guest_cookies(response)
    assert [c["name"] for c in cookies] == [PREFIXED], cookies
    assert await _session_exists(session, owner_a) == 0
    assert await _session_exists(session, owner_b) == 1
    assert await _rows(session, owner_b) == before_b


# --- G-4: junk in the prefixed cookie is an unknown session, never a 500 -------------------------


@pytest.mark.parametrize(
    "junk",
    ["", "x" * 4096, "%E2%82%AC%FF%zz%00"],
    ids=["empty", "4kb", "percent-escapes"],
)
async def test_g4_a_junk_prefixed_cookie_is_unknown_on_get_and_mints_on_post(
    client: AsyncClient, junk: str, caplog: pytest.LogCaptureFixture
) -> None:
    header = {"Cookie": f"{PREFIXED}={junk}"}

    with caplog.at_level(logging.DEBUG):
        got = await client.get("/api/base-cvs", headers=header)
        assert got.status_code == 401, got.text
        assert error_code(got) == "guest_session_expired"

        posted = await client.post(
            "/api/base-cvs",
            files={"file": ("sample.txt", SAMPLE_CV, "text/plain")},
            headers=header,
        )
    # Positive control: the upload logs `cv_extraction.finished`, so capture was live for these
    # requests; without it, an empty log would pass the absence below.
    assert any("cv_extraction" in r.getMessage() for r in caplog.records), caplog.text
    if junk:  # "" is a substring of everything
        assert junk not in caplog.text, "the cookie value reached a log record (G-4)"
    assert posted.status_code == 201, posted.text
    cookies = _guest_cookies(posted)
    assert [c["name"] for c in cookies] == [PREFIXED], cookies
    assert cookies[0]["value"] not in ("", junk)
