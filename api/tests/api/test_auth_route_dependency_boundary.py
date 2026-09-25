"""AC-30's dependency-graph walker (T32): "one credential per route" — no route in this application
depends on both `require_user` and `require_guest_session`.

**Why a walker over `app.routes`, not a manual list of routes.** A manual list is a second copy of
the route table that can drift from the real one the moment somebody adds a route; walking the live
FastAPI application asks the thing itself, so the proof stays true for every route this process ever
serves, added here or in any future slice.

**FastAPI 0.141's `include_router` no longer flattens `app.routes`.** `app.routes` holds the app's own
routes (`/docs`, `/openapi.json`, …) plus one `fastapi.routing._IncludedRouter` per `app.include_router`
call — verified empirically against the installed version, not assumed from an older FastAPI's shape.
The real `APIRoute`s live on `_IncludedRouter.original_router.routes`, so `_iter_api_routes` recurses
through both a plain `APIRouter`'s `.routes` and an `_IncludedRouter`'s `.original_router.routes` —
duck-typed rather than importing `_IncludedRouter` by name, since that class is not part of FastAPI's
public API and importing it by name would be the second thing in this file that can silently drift
from an upgrade.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.infrastructure.api.deps import require_guest_session, require_user
from tailorcraft.infrastructure.api.guest_session import COOKIE_NAME as GUEST_COOKIE_NAME
from tailorcraft.infrastructure.api.guest_session import mint_guest_token
from tailorcraft.infrastructure.settings import Settings

REGISTER_URL = "/api/auth/register"
ME_URL = "/api/auth/me"
BASE_CVS_URL = "/api/base-cvs"


def _iter_api_routes(routes: object) -> Iterator[APIRoute]:
    """Every `APIRoute` reachable from `routes` — a plain iterable of Starlette/FastAPI route or
    router objects, recursing through anything that looks like a router (module docstring)."""
    for route in routes:  # type: ignore[attr-defined]
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _iter_api_routes(route.original_router.routes)
        elif hasattr(route, "routes"):
            yield from _iter_api_routes(route.routes)


def _all_dependency_calls(dependant: Dependant) -> set[object]:
    """Every callable anywhere in `dependant`'s tree — `Depends()` in the path function's own
    signature, in a decorator's `dependencies=[...]`, and in any of those dependencies' own
    `Depends()` parameters, recursively (`require_trusted_origin` sits beside `require_user`/
    `require_guest_session` at this same depth, for instance)."""
    calls: set[object] = set()
    stack = [dependant]
    while stack:
        current = stack.pop()
        for sub in current.dependencies:
            calls.add(sub.call)
            stack.append(sub)
    return calls


def test_the_walker_finds_require_user_on_me_and_require_guest_session_on_base_cvs(
    app: FastAPI,
) -> None:
    """Proves the walker actually discriminates (AC-30's own instruction) before trusting its
    negative result below: it must find `require_user` on `/api/auth/me` and `require_guest_session`
    on `GET /api/base-cvs`, the two routes the rest of this file's behavioural tests exercise."""
    routes_by_path_and_method = {
        (route.path, method): route
        for route in _iter_api_routes(app.routes)
        for method in route.methods or ()
    }

    me_route = routes_by_path_and_method[(ME_URL, "GET")]
    assert require_user in _all_dependency_calls(me_route.dependant)

    base_cvs_route = routes_by_path_and_method[(BASE_CVS_URL, "GET")]
    assert require_guest_session in _all_dependency_calls(base_cvs_route.dependant)


def test_no_route_depends_on_both_require_user_and_require_guest_session(app: FastAPI) -> None:
    """AC-30. Walks every route this application serves; none may depend on both credentials."""
    api_routes = list(_iter_api_routes(app.routes))
    assert len(api_routes) > 5, "the walker found suspiciously few routes — is it even recursing?"

    violations = []
    for route in api_routes:
        calls = _all_dependency_calls(route.dependant)
        if require_user in calls and require_guest_session in calls:
            violations.append((route.path, sorted(route.methods or ())))

    assert violations == [], (
        f"the following routes depend on BOTH require_user and require_guest_session: {violations}"
    )


# --- Behaviour: one credential per route, proved by request/response, not only by the graph -------


async def test_a_guest_route_with_a_valid_bearer_and_no_guest_cookie_behaves_as_without_one(
    client: AsyncClient, settings: Settings
) -> None:
    """AC-30. `GET /api/base-cvs` answers to `require_guest_session` alone — a valid `Authorization:
    Bearer` with no `tc_guest` cookie must not authorize it, and the response must be indistinguishable
    from a request that never sent a bearer at all (same status, same body)."""
    access_token = await _register_and_get_access_token(client, settings)

    without_bearer = await client.get(BASE_CVS_URL)
    with_bearer = await client.get(
        BASE_CVS_URL, headers={"Authorization": f"Bearer {access_token}"}
    )

    assert without_bearer.status_code == 401, without_bearer.text
    assert with_bearer.status_code == without_bearer.status_code
    assert with_bearer.json() == without_bearer.json()


async def test_me_with_a_live_guest_cookie_behaves_as_without_it_and_never_touches_the_guest_row(
    client: AsyncClient, settings: Settings, engine: AsyncEngine
) -> None:
    """AC-30. `GET /api/auth/me` answers to the bearer alone — a live `tc_guest` cookie riding along
    must not change the answer, and (the stronger claim) `require_user` must never even query
    `identity_guest_session`: captured SQL, not merely "the JSON came out the same", the same
    technique `test_purge_database.py`'s AC-16 uses for the purge's own read side."""
    access_token = await _register_and_get_access_token(client, settings)
    minted = mint_guest_token()

    without_cookie = await client.get(ME_URL, headers={"Authorization": f"Bearer {access_token}"})

    captured: list[str] = []

    def _capture(conn: object, cursor: object, statement: str, *_args: object) -> None:
        captured.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        client.cookies.set(GUEST_COOKIE_NAME, minted.token)
        with_cookie = await client.get(ME_URL, headers={"Authorization": f"Bearer {access_token}"})
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    assert without_cookie.status_code == 200, without_cookie.text
    assert with_cookie.status_code == without_cookie.status_code
    assert with_cookie.json() == without_cookie.json()

    joined = "\n".join(captured).lower()
    assert "guest_session" not in joined, (
        f"/me issued a statement naming the guest-session table while a tc_guest cookie was "
        f"present: {joined}"
    )


async def _register_and_get_access_token(client: AsyncClient, settings: Settings) -> str:
    response = await client.post(
        REGISTER_URL,
        json={
            "email": f"t32-ac30-{uuid4().hex}@example.com",
            "password": "correct horse battery staple 9",
        },
        headers={"Origin": settings.public_base_url},
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["access_token"]
    return token
