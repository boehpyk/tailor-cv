"""`POST /api/me/guest-work/claim` — the HTTP contract (slice 2.4, T20; AC-23..AC-31, C-10..C-20).

Written red-first from the spec (feature-spec "The API", failure contract C-10..C-20) against the T19
skeleton, whose handler raises `NotImplementedError` after the dependencies have run. The clients here
use `raise_app_exceptions=False`, so that is a real `500` and a red reads `assert 500 == 200` rather
than an ERROR.

**Traps this file is built around** (technical plan §8):

- **A skeleton satisfies every absence assertion.** "No `Set-Cookie`", "no statement against
  `identity_guest_session`", "rows untouched" all hold of a handler that does nothing. So each absence
  sits in the same test as a discriminating positive (a `200` first, then the claimed rows moved, or a
  captured statement proving the capture works).
- **A bad bearer is already a real `401` in the skeleton** (`require_user` is a dependency), so C-10's
  red comes from the paired positive — the same request with a good bearer — not from the `401` itself.
- **Seeds commit.** The shared `app` fixture rolls back when a request errors; an uncommitted seed
  goes with it (2.3's `fa40793`). `seed_entry` commits; the guest sessions here are minted through the
  real 1.1 upload route, or inserted and committed.
- **Time is a dependency.** The rows are stamped from the fixture `FixedClock` (2026-09-04), which is
  in the past relative to the system clock the routes use: a session seeded from it is *expired*, one
  minted by a real upload is live.
- **No absolute count over a shared table.** Every count is scoped to an id the test created.

`Cache-Control: no-store` is asserted on the responses the handler itself builds (200, 429). A `401`
from the `require_user` dependency and the app-level `503` handler cannot be given a header by the
route; they are not asserted here (reported to the owner with the T20 commit).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi import Response as FastApiResponse
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.infrastructure.api.guest_session import (
    COOKIE_NAME,
    mint_guest_token,
    set_guest_cookie,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.identity.access_tokens import JwtAccessTokens
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    ME_BASE_CVS,
    ME_EXPORT_JOBS,
    ME_POSTINGS,
    ME_RUNS,
    Account,
    Entry,
    bearer,
    captured_statements,
    count_rows,
    error_code,
    mint_guest,
    new_client,
    override_settings,
    register,
    seed_entry,
)
from tests.integration.owners import extracted_cv, succeeded_run

CLAIM_URL = "/api/me/guest-work/claim"
GUEST_BASE_CVS = "/api/base-cvs"
GUEST_POSTINGS = "/api/job-postings"
GUEST_RUNS = "/api/tailoring-runs"
GUEST_EXPORT_JOBS = "/api/export-jobs"

COUNT_KEYS = {
    "base_cvs",
    "job_postings",
    "tailoring_runs",
    "export_jobs",
    "working_copies_dropped",
}
ZEROS = dict.fromkeys(COUNT_KEYS, 0)
# What `mint_guest` (one upload) plus `seed_entry` (a CV, a posting, a run, one export) leave a guest.
ONE_GUESTS_WORK = {
    "base_cvs": 2,
    "job_postings": 1,
    "tailoring_runs": 1,
    "export_jobs": 1,
    "working_copies_dropped": 0,
}
GUEST_TABLES = ("intake_base_cv", "posting_job_posting", "tailoring_run", "export_job")
GUEST_SESSION_TABLE = "identity_guest_session"


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Registration and the claim's own limiter live in Redis; the rollback never reaches it."""


# --- helpers ------------------------------------------------------------------------------------


def _set_cookies(response: Response) -> list[dict[str, str]]:
    """Every `tc_guest` `Set-Cookie`, parsed: `{"name", "value", <lower-cased attribute>: value}`.
    Attributes are parsed, not byte-matched (AC-26): the order and the `expires` text are not ours."""
    parsed: list[dict[str, str]] = []
    for header in response.headers.get_list("set-cookie"):
        first, *attributes = [part.strip() for part in header.split(";")]
        name, _, value = first.partition("=")
        if name != COOKIE_NAME:
            continue
        entry = {"name": name, "value": value.strip('"')}
        for attribute in attributes:
            key, _, attribute_value = attribute.partition("=")
            entry[key.lower()] = attribute_value
        parsed.append(entry)
    return parsed


def _assert_never_mints(response: Response) -> None:
    """No response of this route ever sets a *non-empty* `tc_guest` (AC-25)."""
    for cookie in _set_cookies(response):
        assert cookie["value"] == "", f"the claim minted a guest session: {cookie}"


def _assert_cookie_cleared(response: Response, settings: Settings) -> None:
    cookies = _set_cookies(response)
    assert len(cookies) == 1, f"expected exactly one tc_guest Set-Cookie, got {cookies}"
    cookie = cookies[0]
    assert cookie["value"] == ""
    assert cookie.get("max-age") == "0"
    assert cookie.get("path") == "/"
    assert "httponly" in cookie
    assert cookie.get("samesite", "").lower() == "lax"
    assert ("secure" in cookie) is settings.is_production

    # The deletion must name the attributes the cookie was SET with, or a browser keeps the original.
    reference = FastApiResponse()
    set_guest_cookie(reference, "x", settings)
    set_with = {
        key
        for key in ("path", "httponly", "samesite", "secure")
        if _attribute_present(reference, key)
    }
    cleared_with = {key for key in ("path", "httponly", "samesite", "secure") if key in cookie}
    assert cleared_with == set_with, (
        f"cleared with {sorted(cleared_with)}, set with {sorted(set_with)}"
    )


def _attribute_present(response: FastApiResponse, key: str) -> bool:
    header = response.headers["set-cookie"].lower()
    return any(part.strip().split("=")[0] == key for part in header.split(";")[1:])


def _assert_no_store(response: Response) -> None:
    assert response.headers.get("cache-control") == "no-store", dict(response.headers)


async def _guest_rows(session: AsyncSession, guest: GuestOwner) -> dict[str, int]:
    return {
        table: await count_rows(session, table, guest_session_id=guest.guest_session_id.value)
        for table in GUEST_TABLES
    }


async def _user_rows(session: AsyncSession, account: Account) -> dict[str, int]:
    return {
        table: await count_rows(session, table, user_id=account.user_id.value)
        for table in GUEST_TABLES
    }


async def _session_rows(session: AsyncSession, guest: GuestOwner) -> int:
    return await count_rows(session, GUEST_SESSION_TABLE, id=guest.guest_session_id.value)


async def _guest_with_work(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> tuple[GuestOwner, Entry, str]:
    """A live guest on `client`: a real upload (which mints the session and the cookie) plus a seeded
    CV / posting / run / export. Returns the owner, the entry and the raw `tc_guest` token."""
    guest = await mint_guest(client, session)
    entry = await seed_entry(session, settings, guest, at=clock.now() - timedelta(minutes=10))
    token = client.cookies.get(COOKIE_NAME)
    assert token, "the upload did not leave a tc_guest cookie on the client"
    return guest, entry, token


async def _expired_guest_with_work(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> tuple[GuestOwner, Entry]:
    """A guest session that expired 24 h before the fixture clock (so: long expired for the routes'
    system clock), holding one entry; its token is on `client`."""
    minted = mint_guest_token()
    repository = SqlAlchemyGuestSessionRepository(session)
    expired = GuestSession.start(
        id=repository.next_identity(),
        token_hash=minted.token_hash,
        at=clock.now() - timedelta(hours=48),
        ttl_hours=24,
    )
    await repository.add(expired)
    await session.commit()
    owner = GuestOwner(expired.id)
    entry = await seed_entry(session, settings, owner, at=clock.now() - timedelta(hours=40))
    client.cookies.set(COOKIE_NAME, minted.token)
    return owner, entry


# --- AC-24: the contract -----------------------------------------------------------------------


async def test_ac24_the_success_body_is_exactly_the_five_counts_and_is_no_store(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    account = await register(client, settings)
    await _guest_with_work(client, session, settings, clock)

    response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == COUNT_KEYS
    assert body == ONE_GUESTS_WORK
    _assert_no_store(response)


async def test_ac24_a_request_body_is_ignored(client: AsyncClient, settings: Settings) -> None:
    """C-19: no body parameter, so nothing to validate and no 422 is possible."""
    account = await register(client, settings)

    response = await client.post(
        CLAIM_URL,
        content=b"{ this is not json",
        headers={**account.headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == ZEROS


# --- AC-25 / C-10 / C-11: order, and "mint nothing" ---------------------------------------------


@pytest.mark.parametrize("kind", ["missing", "forged", "expired"])
async def test_ac25_a_bad_bearer_is_401_before_the_guest_table_is_read_and_a_good_one_reads_it(
    client: AsyncClient,
    session: AsyncSession,
    settings: Settings,
    clock: FixedClock,
    kind: str,
) -> None:
    """C-10. The request carries a LIVE `tc_guest`; a bad bearer must be refused by the dependency
    with **no statement naming `identity_guest_session`**, and with no cookie set or cleared.

    The discriminating positive is in the same test: the same request with a *good* bearer is a `200`
    **and** its statements do name the guest table — otherwise the capture proves nothing (a skeleton
    that does nothing already satisfies the absence)."""
    account = await register(client, settings)
    guest, _, _ = await _guest_with_work(client, session, settings, clock)
    headers: dict[str, str] = {}
    if kind == "forged":
        headers = bearer("not.a.jwt")
    elif kind == "expired":
        tokens = JwtAccessTokens(
            settings.jwt_signing_key.get_secret_value(),
            timedelta(minutes=settings.access_token_ttl_minutes),
        )
        headers = bearer(tokens.issue(account.user_id, clock.now()).token)

    with captured_statements(session) as refused_statements:
        refused = await client.post(CLAIM_URL, headers=headers)

    assert refused.status_code == 401, refused.text
    assert error_code(refused) == "invalid_access_token"
    assert "www-authenticate" in {name.lower() for name in refused.headers}
    assert _set_cookies(refused) == []
    assert not any(GUEST_SESSION_TABLE in s.lower() for s in refused_statements), refused_statements
    assert await _session_rows(session, guest) == 1

    with captured_statements(session) as accepted_statements:
        accepted = await client.post(CLAIM_URL, headers=account.headers)

    assert accepted.status_code == 200, accepted.text
    assert any(GUEST_SESSION_TABLE in s.lower() for s in accepted_statements), (
        "the capture cannot see the guest-session table, so the absence above proves nothing"
    )


async def test_ac25_c11_a_valid_bearer_for_an_erased_user_is_401_not_signed_in_and_reads_no_session(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    account = await register(client, settings)
    guest, _, _ = await _guest_with_work(client, session, settings, clock)
    await session.execute(
        text("DELETE FROM identity_user WHERE id = :id"), {"id": account.user_id.value}
    )
    await session.commit()

    with captured_statements(session) as statements:
        response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 401, response.text
    assert error_code(response) == "not_signed_in"
    assert _set_cookies(response) == []
    assert not any(GUEST_SESSION_TABLE in s.lower() for s in statements), statements
    assert await _session_rows(session, guest) == 1
    assert (await _guest_rows(session, guest))["intake_base_cv"] == 2


async def test_ac25_no_path_through_the_route_mints_a_guest_session(
    client: AsyncClient, session: AsyncSession, settings: Settings
) -> None:
    """A signed-in user with **no** cookie and no guest session anywhere: the `200` must neither set a
    cookie nor insert a session row (`resolve_or_start_guest_session` would do both)."""
    account = await register(client, settings)
    before = await count_rows(session, GUEST_SESSION_TABLE)

    response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json() == ZEROS
    _assert_never_mints(response)
    assert await count_rows(session, GUEST_SESSION_TABLE) == before


# --- AC-26 / C-15: the happy path ----------------------------------------------------------------


async def test_ac26_a_live_session_is_claimed_whole_and_its_cookie_cleared(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    account = await register(client, settings)
    guest, _, old_token = await _guest_with_work(client, session, settings, clock)
    assert await _guest_rows(session, guest) == {
        "intake_base_cv": 2,
        "posting_job_posting": 1,
        "tailoring_run": 1,
        "export_job": 1,
    }

    response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json() == ONE_GUESTS_WORK
    assert await _guest_rows(session, guest) == dict.fromkeys(GUEST_TABLES, 0)
    assert await _user_rows(session, account) == {
        "intake_base_cv": 2,
        "posting_job_posting": 1,
        "tailoring_run": 1,
        "export_job": 1,
    }
    assert await _session_rows(session, guest) == 0
    _assert_cookie_cleared(response, settings)

    # The old token authenticates nothing: put it back by hand (a browser would have dropped it).
    client.cookies.set(COOKIE_NAME, old_token)
    stale = await client.get(GUEST_BASE_CVS)
    assert stale.status_code == 401, stale.text
    assert error_code(stale) == "guest_session_expired"


async def test_ac26_the_cleared_cookie_carries_the_production_attributes_in_production(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    await _guest_with_work(client, session, settings, clock)
    production = override_settings(app, settings, app_env="production")

    response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    assert production.is_production
    assert "secure" in _set_cookies(response)[0]
    _assert_cookie_cleared(response, production)


async def test_ac26_a_live_session_that_owns_nothing_is_deleted_and_answers_zeros(
    client: AsyncClient, session: AsyncSession, settings: Settings
) -> None:
    """C-16: an unused cookie. Minted through the real route, then its one CV removed, so the session
    is live and owns nothing."""
    account = await register(client, settings)
    guest = await mint_guest(client, session)
    await session.execute(
        text("DELETE FROM intake_base_cv WHERE guest_session_id = :id"),
        {"id": guest.guest_session_id.value},
    )
    await session.commit()
    assert await _session_rows(session, guest) == 1

    response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json() == ZEROS
    assert await _session_rows(session, guest) == 0
    _assert_cookie_cleared(response, settings)


# --- AC-27 / C-12, C-13, C-14: nothing to claim --------------------------------------------------


async def test_ac27_no_cookie_is_200_zeros_and_no_set_cookie_and_a_bystanders_work_stays(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    async with new_client(app) as bystander_client:
        bystander = await mint_guest(bystander_client, session)
        await seed_entry(session, settings, bystander, at=clock.now() - timedelta(minutes=10))
    client.cookies.delete(COOKIE_NAME)

    response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json() == ZEROS
    assert _set_cookies(response) == [], "a cookie was set (or cleared) though none was presented"
    assert (await _guest_rows(session, bystander))["intake_base_cv"] == 2
    assert await _session_rows(session, bystander) == 1
    assert await _user_rows(session, account) == dict.fromkeys(GUEST_TABLES, 0)


async def test_ac27_an_unknown_cookie_is_200_zeros_and_is_cleared_and_a_bystander_stays(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    async with new_client(app) as bystander_client:
        bystander = await mint_guest(bystander_client, session)
    client.cookies.set(COOKIE_NAME, mint_guest_token().token)

    response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json() == ZEROS
    _assert_cookie_cleared(response, settings)
    assert (await _guest_rows(session, bystander))["intake_base_cv"] == 1
    assert await _session_rows(session, bystander) == 1


async def test_ac27_an_expired_session_is_200_zeros_its_rows_untouched_and_the_cookie_cleared(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    """C-14: an expired session's rows are the purge's, not the claim's."""
    account = await register(client, settings)
    guest, _ = await _expired_guest_with_work(client, session, settings, clock)
    before = await _guest_rows(session, guest)
    assert before == {
        "intake_base_cv": 1,
        "posting_job_posting": 1,
        "tailoring_run": 1,
        "export_job": 1,
    }

    response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json() == ZEROS
    _assert_cookie_cleared(response, settings)
    assert await _guest_rows(session, guest) == before
    assert await _session_rows(session, guest) == 1
    assert await _user_rows(session, account) == dict.fromkeys(GUEST_TABLES, 0)


# --- AC-28 / C-18: idempotent in effect ---------------------------------------------------------


async def test_ac28_repeating_a_claim_is_200_zeros_and_no_row_moves_twice(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    account = await register(client, settings)
    _, _, old_token = await _guest_with_work(client, session, settings, clock)
    first = await client.post(CLAIM_URL, headers=account.headers)
    assert first.status_code == 200, first.text
    assert first.json() == ONE_GUESTS_WORK
    after_first = await _user_rows(session, account)
    assert after_first["intake_base_cv"] == 2

    client.cookies.set(COOKIE_NAME, old_token)  # the retry presents the same, now-dead, cookie
    second = await client.post(CLAIM_URL, headers=account.headers)

    assert second.status_code == 200, second.text
    assert second.json() == ZEROS
    assert await _user_rows(session, account) == after_first


# --- AC-29 / C-17, C-8: the limiter -------------------------------------------------------------


async def test_ac29_over_the_limit_is_429_rate_limited_with_retry_after_and_nothing_moves(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    clock: FixedClock,
) -> None:
    override_settings(app, settings, guest_work_claim_rate_limit_per_hour=1)
    account = await register(client, settings)
    first = await client.post(CLAIM_URL, headers=account.headers)  # no cookie: spends the one slot
    assert first.status_code == 200, first.text
    guest, _, _ = await _guest_with_work(client, session, settings, clock)

    second = await client.post(CLAIM_URL, headers=account.headers)

    assert second.status_code == 429, second.text
    assert error_code(second) == "rate_limited"
    assert "retry-after" in {name.lower() for name in second.headers}
    _assert_no_store(second)
    assert _set_cookies(second) == []
    assert (await _guest_rows(session, guest))["intake_base_cv"] == 2
    assert await _session_rows(session, guest) == 1


async def test_ac29_the_bucket_is_the_users_not_a_global_one(
    client: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    override_settings(app, settings, guest_work_claim_rate_limit_per_hour=1)
    alice = await register(client, settings)
    bob = await register(client, settings)

    first = await client.post(CLAIM_URL, headers=alice.headers)
    other = await client.post(CLAIM_URL, headers=bob.headers)
    again = await client.post(CLAIM_URL, headers=alice.headers)

    assert first.status_code == 200, first.text
    assert other.status_code == 200, other.text
    assert again.status_code == 429, again.text


async def test_ac29_c8_with_redis_down_the_claim_proceeds(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    clock: FixedClock,
) -> None:
    """The claim limiter fails OPEN: a claim costs only our own database."""
    account = await register(client, settings)
    guest, _, _ = await _guest_with_work(client, session, settings, clock)
    override_settings(app, settings, redis_url="redis://127.0.0.1:1/0")

    response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json() == ONE_GUESTS_WORK
    assert await _guest_rows(session, guest) == dict.fromkeys(GUEST_TABLES, 0)


# --- AC-30 / C-9: Postgres down / the commit fails ----------------------------------------------


async def test_ac30_a_failed_commit_is_503_service_unavailable_nothing_moved_no_cookie_cleared(
    client: AsyncClient,
    session: AsyncSession,
    settings: Settings,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = await register(client, settings)
    guest, _, _ = await _guest_with_work(client, session, settings, clock)

    async def _refuse() -> None:
        raise SQLAlchemyError("simulated commit failure (AC-30)")

    monkeypatch.setattr(session, "commit", _refuse)

    response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"
    assert _set_cookies(response) == []
    monkeypatch.undo()
    assert (await _guest_rows(session, guest))["intake_base_cv"] == 2
    assert await _user_rows(session, account) == dict.fromkeys(GUEST_TABLES, 0)
    assert await _session_rows(session, guest) == 1


# --- AC-31: the claimed rows answer only to the account ----------------------------------------


async def test_ac31_claimed_ids_are_served_on_the_me_routes_with_null_expiry_and_not_on_guest_routes(
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    clock: FixedClock,
) -> None:
    account = await register(client, settings)
    guest, entry, old_token = await _guest_with_work(client, session, settings, clock)
    job = entry.jobs[0]
    assert (await client.post(CLAIM_URL, headers=account.headers)).status_code == 200

    cvs = await client.get(ME_BASE_CVS, headers=account.headers)
    assert cvs.status_code == 200, cvs.text
    assert str(entry.cv_id.value) in {item["id"] for item in cvs.json()["items"]}

    postings = await client.get(ME_POSTINGS, headers=account.headers)
    assert postings.status_code == 200, postings.text
    (posting,) = [p for p in postings.json()["items"] if p["id"] == str(entry.posting_id.value)]
    assert posting["expires_at"] is None

    history = await client.get(ME_RUNS, headers=account.headers)
    assert history.status_code == 200, history.text
    assert str(entry.run_id.value) in {item["id"] for item in history.json()["items"]}
    run = await client.get(entry.run_url, headers=account.headers)
    assert run.status_code == 200, run.text
    assert run.json()["expires_at"] is None

    export = await client.get(f"{ME_EXPORT_JOBS}/{job.id.value}", headers=account.headers)
    assert export.status_code == 200, export.text
    assert export.json()["expires_at"] is None

    # On the guest routes: the old (dead) cookie is 401, and a different live guest's is 404 — never 200.
    guest_urls = [
        f"{GUEST_POSTINGS}/{entry.posting_id.value}",
        f"{GUEST_RUNS}/{entry.run_id.value}",
        f"{GUEST_EXPORT_JOBS}/{job.id.value}",
    ]
    client.cookies.set(COOKIE_NAME, old_token)
    for url in guest_urls:
        dead = await client.get(url)
        assert dead.status_code == 401, (url, dead.text)
    async with new_client(app) as other_guest:
        await mint_guest(other_guest, session)
        for url in guest_urls:
            foreign = await other_guest.get(url)
            assert foreign.status_code == 404, (url, foreign.text)
        listed = await other_guest.get(GUEST_BASE_CVS)
        assert listed.status_code == 200, listed.text
        assert str(entry.cv_id.value) not in {item["id"] for item in listed.json()["items"]}
    assert await _session_rows(session, guest) == 0


# --- C-20: caps bound creation, not transfer -----------------------------------------------------


async def test_c20_a_claim_that_leaves_the_account_above_a_cap_is_still_200(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    account = await register(client, settings)
    cvs = SqlAlchemyBaseCvRepository(session)
    for _ in range(settings.max_saved_base_cvs_per_user):
        await cvs.add(extracted_cv(account.owner, clock.now() - timedelta(hours=1)))
    await session.commit()
    await _guest_with_work(client, session, settings, clock)

    response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    assert response.json()["base_cvs"] == 2
    assert await count_rows(session, "intake_base_cv", user_id=account.user_id.value) == (
        settings.max_saved_base_cvs_per_user + 2
    )


# --- AC-23: no ownership graph crosses owners after a claim -------------------------------------

_CROSSING_COUNT = text(
    """
    SELECT
      (SELECT count(*) FROM tailoring_run r JOIN intake_base_cv c ON c.id = r.base_cv_id
        WHERE r.id = :run
          AND (r.user_id IS DISTINCT FROM c.user_id
               OR r.guest_session_id IS DISTINCT FROM c.guest_session_id))
    + (SELECT count(*) FROM tailoring_run r JOIN posting_job_posting p ON p.id = r.job_posting_id
        WHERE r.id = :run
          AND (r.user_id IS DISTINCT FROM p.user_id
               OR r.guest_session_id IS DISTINCT FROM p.guest_session_id))
    + (SELECT count(*) FROM export_job e JOIN tailoring_run r ON r.id = e.tailoring_run_id
        WHERE r.id = :run
          AND (e.user_id IS DISTINCT FROM r.user_id
               OR e.guest_session_id IS DISTINCT FROM r.guest_session_id))
    """
)


async def _crossings(session: AsyncSession, run_id: UUID) -> int:
    return int((await session.execute(_CROSSING_COUNT, {"run": run_id})).scalar_one())


async def test_ac23_the_crossing_query_can_see_a_crossing(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    """Its own positive control: a user-owned run over a guest-owned CV and posting is two crossings.
    Without this, the zero asserted below could be a query that finds nothing."""
    account = await register(client, settings)
    _, entry, _ = await _guest_with_work(client, session, settings, clock)
    crossing = succeeded_run(
        account.owner,
        clock.now(),
        base_cv_id=entry.cv_id,
        job_posting_id=entry.posting_id,
    )
    await SqlAlchemyTailoringRunRepository(session).add(crossing)
    await session.commit()

    assert await _crossings(session, crossing.id.value) == 2


async def test_ac23_after_a_claim_no_run_posting_cv_or_export_has_a_different_owner_from_its_run(
    client: AsyncClient, session: AsyncSession, settings: Settings, clock: FixedClock
) -> None:
    account = await register(client, settings)
    _, entry, _ = await _guest_with_work(client, session, settings, clock)

    response = await client.post(CLAIM_URL, headers=account.headers)

    assert response.status_code == 200, response.text
    assert await count_rows(session, "tailoring_run", id=entry.run_id.value) == 1
    assert (
        await count_rows(
            session, "tailoring_run", id=entry.run_id.value, user_id=account.user_id.value
        )
        == 1
    ), "the claim did not move the run, so a zero crossing count would be vacuous"
    assert await _crossings(session, entry.run_id.value) == 0
