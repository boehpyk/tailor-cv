"""Slice 4.1, T15 RED: the admin firewall's HTTP contract (AC-14 ... AC-21, R-6).

Written from the spec, against the T14 skeleton (`require_admin` and `access` raise
`NotImplementedError`). The app is a **concurrent** one - a real session and connection per request
- because AC-18 claims "the next request sees it" and only a role change committed on a *separate*
connection can say so (2.2's AC-27: on the request's own session an uncommitted write is already
visible). Every user it seeds is real and deleted in teardown (the cascades take the logins).

Roles are granted and revoked with a raw `UPDATE` committed on `engine.connect()`, not through
`ChangeUserRole`: the firewall's contract is "reads the row at request time", and a raw statement is
the one way to change that row that shares no code with anything under test.

`raise_app_exceptions=False` turns the skeleton's `NotImplementedError` into a real 500 response, so
a red reads `assert 500 == 204`, an assertion, rather than an ERROR. `NotImplementedError` subclasses
`RuntimeError`, so nothing here uses `pytest.raises(RuntimeError)`.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import AsyncClient, Response
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.api.guest_session import COOKIE_NAME as GUEST_COOKIE_NAME
from tailorcraft.infrastructure.api.guest_session import mint_guest_token
from tailorcraft.infrastructure.identity.access_tokens import JwtAccessTokens
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.database import create_session_factory
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    assert_test_database,
    bearer,
    build_concurrent_app,
    error_code,
    new_client,
    override_settings,
    seed_user_and_sign_in,
)

ACCESS_URL = "/api/admin/access"
UNMATCHED_URL = "/api/admin/no-such-route"
ME_URL = "/api/auth/me"
CHALLENGE = 'Bearer error="invalid_token"'


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """Login limiters live in Redis, which the database rollback does not reach."""


@pytest.fixture
def world_app(
    settings: Settings, engine: AsyncEngine, password_hasher: Argon2PasswordHasher
) -> FastAPI:
    assert_test_database(settings)
    return build_concurrent_app(settings, engine, password_hasher)


@pytest_asyncio.fixture
async def http(world_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(world_app) as client:
        yield client


@pytest_asyncio.fixture
async def seeded_ids(engine: AsyncEngine, settings: Settings) -> AsyncIterator[list[UUID]]:
    """Ids the test created; deleted afterwards (the logins cascade)."""
    ids: list[UUID] = []
    yield ids
    assert_test_database(settings)
    async with engine.begin() as conn:
        for user_id in ids:
            await conn.execute(text("DELETE FROM identity_user WHERE id = :id"), {"id": user_id})


async def _account(
    http: AsyncClient, settings: Settings, seeded_ids: list[UUID], *, admin: bool = False
) -> tuple[str, UUID]:
    token, user_id = await seed_user_and_sign_in(http, settings)
    seeded_ids.append(user_id)
    if admin:
        await _set_role(http, user_id, "admin")
    return token, user_id


async def _set_role(http: AsyncClient, user_id: UUID, role: str) -> None:
    """Commit a role change on a connection of its own, as the CLI would from another process."""
    engine: AsyncEngine = http._transport.app.state.engine  # type: ignore[attr-defined]
    async with engine.connect() as conn:
        await conn.execute(
            text("UPDATE identity_user SET role = :role WHERE id = :id"),
            {"role": role, "id": user_id},
        )
        await conn.commit()


@contextmanager
def _statements(engine: AsyncEngine) -> Iterator[list[str]]:
    seen: list[str] = []

    def capture(conn: Connection, cursor: object, statement: str, *args: object) -> None:
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)


def _spy_on_access_handler(app: FastAPI) -> list[int]:
    """Count calls to the route's endpoint.

    FastAPI 0.141 serves `_EffectiveRouteContext`s built from `route.endpoint` and cached per
    `_IncludedRouter`, so patching an `APIRoute`'s dependant after the build is never seen. Wrap the
    endpoint, then drop the cached contexts so the next request rebuilds them from the wrapper.
    """
    calls: list[int] = []
    routers: list[Any] = []

    def routes(items: Any) -> Iterator[APIRoute]:
        for route in items:
            if isinstance(route, APIRoute):
                yield route
            elif hasattr(route, "original_router"):
                routers.append(route)
                yield from routes(route.original_router.routes)
            elif hasattr(route, "routes"):
                yield from routes(route.routes)

    (route,) = [
        r for r in routes(app.routes) if r.path == ACCESS_URL and "GET" in (r.methods or set())
    ]
    real = route.endpoint

    @functools.wraps(real)
    async def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return await real(*args, **kwargs)

    route.endpoint = spy
    for included in routers:
        included._effective_candidates_version = None
        included._effective_low_priority_routes_version = None
    return calls


def _comparable(response: Response) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k != "date"}


def _access_token(
    settings: Settings, user_id: UUID, *, key: str | None = None, at: datetime
) -> str:
    ttl = timedelta(minutes=settings.access_token_ttl_minutes)
    tokens = JwtAccessTokens(key or settings.jwt_signing_key.get_secret_value(), ttl)
    return tokens.issue(UserId(user_id), at).token


# ---------------------------------------------------------------------------------------------
# AC-14 / AC-15
# ---------------------------------------------------------------------------------------------


async def test_an_admin_is_answered_204_empty_with_no_store(
    http: AsyncClient, settings: Settings, seeded_ids: list[UUID]
) -> None:
    token, _ = await _account(http, settings, seeded_ids, admin=True)

    response = await http.get(ACCESS_URL, headers=bearer(token))

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert response.headers.get("cache-control") == "no-store"


async def test_a_plain_user_gets_a_404_byte_identical_to_an_unmatched_path(
    http: AsyncClient, settings: Settings, seeded_ids: list[UUID]
) -> None:
    token, _ = await _account(http, settings, seeded_ids)

    refused = await http.get(ACCESS_URL, headers=bearer(token))
    unmatched = await http.get(UNMATCHED_URL, headers=bearer(token))

    assert unmatched.status_code == 404, unmatched.text  # the live comparison side is itself sane
    assert refused.status_code == 404, refused.text
    assert refused.content == unmatched.content
    assert refused.json() == {"detail": "Not Found"}
    assert _comparable(refused) == _comparable(unmatched)
    assert "no-store" not in refused.headers.get("cache-control", "")
    assert "no-store" not in unmatched.headers.get("cache-control", "")


async def test_the_handler_runs_for_an_admin_and_never_for_a_plain_user(
    http: AsyncClient, world_app: FastAPI, settings: Settings, seeded_ids: list[UUID]
) -> None:
    admin_token, _ = await _account(http, settings, seeded_ids, admin=True)
    user_token, _ = await _account(http, settings, seeded_ids)
    calls = _spy_on_access_handler(world_app)

    as_admin = await http.get(ACCESS_URL, headers=bearer(admin_token))
    assert as_admin.status_code == 204, as_admin.text
    assert calls == [1], "positive control: the spy must see the admin's call"

    as_user = await http.get(ACCESS_URL, headers=bearer(user_token))
    assert as_user.status_code == 404, as_user.text
    assert calls == [1], "the handler ran for a plain user"


# ---------------------------------------------------------------------------------------------
# AC-16 - a bad bearer is a 401 before any role read
# ---------------------------------------------------------------------------------------------

_LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)
_BAD_BEARERS: list[tuple[str, Callable[[Settings], dict[str, str]]]] = [
    ("no-header", lambda s: {}),
    ("not-bearer-scheme", lambda s: {"Authorization": "Basic dXNlcjpwYXNz"}),
    (
        "expired",
        lambda s: bearer(_access_token(s, uuid4(), at=_LONG_AGO)),
    ),
    (
        "other-key",
        lambda s: bearer(
            _access_token(
                s, uuid4(), key="another-signing-key-of-at-least-32-bytes!!", at=datetime.now(UTC)
            )
        ),
    ),
]


@pytest.mark.parametrize(("name", "headers_for"), _BAD_BEARERS, ids=[n for n, _ in _BAD_BEARERS])
async def test_a_bad_bearer_is_401_never_404_and_reads_no_user_row(
    http: AsyncClient,
    engine: AsyncEngine,
    settings: Settings,
    name: str,
    headers_for: Callable[[Settings], dict[str, str]],
) -> None:
    headers = headers_for(settings)
    with _statements(engine) as seen:
        response = await http.get(ACCESS_URL, headers=headers)
    on_me = await http.get(ME_URL, headers=headers)

    assert response.status_code == 401, f"{name}: {response.text}"
    assert error_code(response) == "invalid_access_token"
    assert response.headers.get("www-authenticate") == CHALLENGE
    assert response.content == on_me.content, "the body must be /me's for the same input"
    assert [s for s in seen if "identity_user" in s] == []


# ---------------------------------------------------------------------------------------------
# AC-17 - the account is gone
# ---------------------------------------------------------------------------------------------


async def test_a_valid_bearer_whose_user_row_is_gone_is_401_not_signed_in(
    http: AsyncClient, engine: AsyncEngine, settings: Settings, seeded_ids: list[UUID]
) -> None:
    token, user_id = await _account(http, settings, seeded_ids, admin=True)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM identity_user WHERE id = :id"), {"id": user_id})

    response = await http.get(ACCESS_URL, headers=bearer(token))

    assert response.status_code == 401, response.text
    assert error_code(response) == "not_signed_in"


# ---------------------------------------------------------------------------------------------
# AC-18 - promotion and demotion take effect on the next request, with the same token
# ---------------------------------------------------------------------------------------------


async def test_a_promotion_and_a_demotion_apply_to_the_next_request_with_the_same_token(
    http: AsyncClient, settings: Settings, seeded_ids: list[UUID]
) -> None:
    token, user_id = await _account(http, settings, seeded_ids)

    before = await http.get(ACCESS_URL, headers=bearer(token))
    await _set_role(http, user_id, "admin")
    promoted = await http.get(ACCESS_URL, headers=bearer(token))
    await _set_role(http, user_id, "user")
    demoted = await http.get(ACCESS_URL, headers=bearer(token))

    assert before.status_code == 404, before.text
    assert promoted.status_code == 204, promoted.text
    assert demoted.status_code == 404, demoted.text


# ---------------------------------------------------------------------------------------------
# AC-19 - Postgres down
# ---------------------------------------------------------------------------------------------


async def test_a_failing_role_read_is_503_never_404_or_204(
    http: AsyncClient,
    settings: Settings,
    seeded_ids: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fault is below the repository: `AsyncSession.execute` is what it calls, so
    `require_admin`'s real path stands between the fault and the response."""
    token, _ = await _account(http, settings, seeded_ids, admin=True)

    async def _down(self: AsyncSession, *args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("simulated: postgres unavailable (AC-19)")

    monkeypatch.setattr(AsyncSession, "execute", _down)

    response = await http.get(ACCESS_URL, headers=bearer(token))

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"


# ---------------------------------------------------------------------------------------------
# AC-20 - Redis down
# ---------------------------------------------------------------------------------------------


async def test_with_redis_unreachable_an_admin_still_gets_204_and_a_user_404(
    http: AsyncClient, world_app: FastAPI, settings: Settings, seeded_ids: list[UUID]
) -> None:
    admin_token, _ = await _account(http, settings, seeded_ids, admin=True)
    user_token, _ = await _account(http, settings, seeded_ids)
    override_settings(world_app, settings, redis_url="redis://127.0.0.1:1/0")

    as_admin = await http.get(ACCESS_URL, headers=bearer(admin_token))
    as_user = await http.get(ACCESS_URL, headers=bearer(user_token))

    assert as_admin.status_code == 204, as_admin.text
    assert as_user.status_code == 404, as_user.text


# ---------------------------------------------------------------------------------------------
# AC-21 - one credential: the guest cookie is never read
# ---------------------------------------------------------------------------------------------


async def _live_guest_cookie(engine: AsyncEngine) -> str:
    factory = create_session_factory(engine)
    async with factory() as session:
        sessions = SqlAlchemyGuestSessionRepository(session)
        minted = mint_guest_token()
        await sessions.add(
            GuestSession.start(
                sessions.next_identity(),
                minted.token_hash,
                datetime.now(UTC).replace(microsecond=0),
                24,
            )
        )
        await session.commit()
    return minted.token


async def test_a_live_guest_cookie_without_a_bearer_is_401_invalid_access_token(
    http: AsyncClient, engine: AsyncEngine
) -> None:
    http.cookies.set(GUEST_COOKIE_NAME, await _live_guest_cookie(engine))

    response = await http.get(ACCESS_URL)

    assert response.status_code == 401, response.text
    assert error_code(response) == "invalid_access_token"


async def test_a_guest_cookie_beside_an_admin_bearer_changes_nothing_and_is_never_read(
    http: AsyncClient, engine: AsyncEngine, settings: Settings, seeded_ids: list[UUID]
) -> None:
    token, _ = await _account(http, settings, seeded_ids, admin=True)
    http.cookies.set(GUEST_COOKIE_NAME, await _live_guest_cookie(engine))

    with _statements(engine) as seen:
        response = await http.get(ACCESS_URL, headers=bearer(token))

    assert response.status_code == 204, response.text
    assert [s for s in seen if "identity_guest_session" in s] == []


# ---------------------------------------------------------------------------------------------
# R-6 - a wrong method is routing's 405, for anyone
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("who", ["anonymous", "user", "admin"])
async def test_a_wrong_method_on_the_admin_path_is_405_with_allow_get_for_anyone(
    http: AsyncClient, settings: Settings, seeded_ids: list[UUID], method: str, who: str
) -> None:
    headers: dict[str, str] = {}
    if who != "anonymous":
        token, _ = await _account(http, settings, seeded_ids, admin=(who == "admin"))
        headers = bearer(token)

    response = await http.request(method, ACCESS_URL, headers=headers)

    assert response.status_code == 405, response.text
    assert "GET" in response.headers.get("allow", "")


# ---------------------------------------------------------------------------------------------
# AC-24 (T17): admin includes user
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["/api/me/tailoring-runs", "/api/me/board", "/api/me/base-cvs"])
async def test_an_admin_gets_the_same_200_from_the_user_routes_as_a_user_does(
    http: AsyncClient, settings: Settings, seeded_ids: list[UUID], url: str
) -> None:
    user_token, _ = await _account(http, settings, seeded_ids)
    admin_token, _ = await _account(http, settings, seeded_ids, admin=True)

    as_user = await http.get(url, headers=bearer(user_token))
    as_admin = await http.get(url, headers=bearer(admin_token))

    assert as_user.status_code == 200, as_user.text
    assert as_admin.status_code == 200, as_admin.text
    assert as_admin.json() == as_user.json()


# ---------------------------------------------------------------------------------------------
# AC-25 (T17): the refusal's one log line
# ---------------------------------------------------------------------------------------------

REFUSED = "identity.admin_access_refused"


def _refusal_lines(caplog: pytest.LogCaptureFixture, since: int) -> list[str]:
    return [r.getMessage() for r in caplog.records[since:] if REFUSED in r.getMessage()]


async def test_a_refused_plain_user_logs_one_line_with_the_route_template_and_no_pii(
    http: AsyncClient,
    settings: Settings,
    seeded_ids: list[UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:
    token, user_id = await _account(http, settings, seeded_ids)

    with caplog.at_level(logging.DEBUG):
        mark = len(caplog.records)
        response = await http.get(ACCESS_URL, headers=bearer(token))
        lines = _refusal_lines(caplog, mark)
        everything = " ".join(r.getMessage() for r in caplog.records[mark:])

    assert response.status_code == 404
    assert len(lines) == 1, lines
    (line,) = lines
    assert str(user_id) in line
    assert "GET" in line
    assert ACCESS_URL in line
    assert token not in everything
    assert "@" not in everything  # no email address anywhere in this request's output


async def test_the_line_carries_the_route_template_never_the_raw_path(
    http: AsyncClient,
    world_app: FastAPI,
    settings: Settings,
    seeded_ids: list[UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The only admin route today has no path parameter, so a throwaway templated route on the same
    router (mounted on a copy of the production firewall) is the only way to tell a template from a
    raw path. The raw path carries a marker the log must not."""
    from fastapi import APIRouter, Depends

    from tailorcraft.infrastructure.api.deps import require_admin

    probe = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

    @probe.get("/things/{thing_id}")
    async def thing(thing_id: str) -> None: ...

    world_app.include_router(probe)
    token, _ = await _account(http, settings, seeded_ids)
    marker = f"raw-{uuid4().hex}"

    with caplog.at_level(logging.DEBUG):
        mark = len(caplog.records)
        response = await http.get(f"/api/admin/things/{marker}", headers=bearer(token))
        lines = _refusal_lines(caplog, mark)

    assert response.status_code == 404
    assert len(lines) == 1, lines
    assert "/api/admin/things/{thing_id}" in lines[0]
    assert marker not in lines[0]


async def test_an_admin_and_a_bad_bearer_log_no_refusal_line(
    http: AsyncClient,
    settings: Settings,
    seeded_ids: list[UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:
    admin_token, _ = await _account(http, settings, seeded_ids, admin=True)
    with caplog.at_level(logging.DEBUG):
        mark = len(caplog.records)
        allowed = await http.get(ACCESS_URL, headers=bearer(admin_token))
        anonymous = await http.get(ACCESS_URL)
        garbage = await http.get(ACCESS_URL, headers=bearer("not.a.token"))
        lines = _refusal_lines(caplog, mark)

    assert allowed.status_code == 204
    assert anonymous.status_code == 401
    assert garbage.status_code == 401
    assert lines == []
