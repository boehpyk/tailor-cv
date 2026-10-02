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

import ast
import inspect
import sys
import textwrap
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.infrastructure.api import routers as _routers_package
from tailorcraft.infrastructure.api.deps import (
    require_guest_session,
    require_user,
    resolve_or_start_guest_session,
)
from tailorcraft.infrastructure.api.guest_session import COOKIE_NAME as GUEST_COOKIE_NAME
from tailorcraft.infrastructure.api.guest_session import mint_guest_token
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import seed_user_and_sign_in

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
    """A signed-in user's bearer. Slice 2.5 (T27): registering no longer returns one (202, empty), so
    the account is seeded through the repository and signed in through the real login route
    (`me_support.seed_user_and_sign_in`). The name is kept: the callers' claim is about the bearer."""
    token, _user_id = await seed_user_and_sign_in(
        client, settings, email=f"t32-ac30-{uuid4().hex}@example.com"
    )
    return token


# ---------------------------------------------------------------------------------------------
# AC-24 (slice 2.2, T19) — "one credential per route, except a named transfer route" (ADR-0008
# amendment (f)). The dependency-graph walker above cannot see a call made from *inside* a handler
# body (`resolve_or_start_guest_session` in `copy_saved_base_cv`, §0.3's whole point: calling it
# from the body rather than as a sibling `Depends` is what keeps a 422 from minting a session). This
# extends the walker with an AST scan of the router modules for direct calls to
# `resolve_or_start_guest_session` / `read_guest_token`, and pins the exception set — the routes that
# depend on `require_user` **and** touch `tc_guest` by either mechanism — to exactly one route.
# ---------------------------------------------------------------------------------------------

_GUEST_TOUCHING_CALL_NAMES = frozenset(
    {"resolve_or_start_guest_session", "read_guest_token", "clear_guest_cookie"}
)
_ROUTERS_PACKAGE_PREFIX = "tailorcraft.infrastructure.api.routers"


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


class _FunctionCallCollector(ast.NodeVisitor):
    """Attributes **every** call in a module to its nearest enclosing function — not only a direct
    call to one of `_GUEST_TOUCHING_CALL_NAMES` — so a call inside a nested helper is never mistaken
    for one made directly by a route handler, and vice versa.

    **/verify round 1 MINOR.** Recording every call name (not only the two guest-touching ones) is
    what makes `_transitive_guest_touchers` below possible: a route handler that reaches
    `resolve_or_start_guest_session` only through a module-local helper — never in its own body —
    used to be invisible to this scan entirely, because the old version of this class only ever
    populated `calls_by_function[name]` when `name` was itself one of the two watched names."""

    def __init__(self) -> None:
        self.calls_by_function: dict[str, set[str]] = {}
        self._stack: list[str] = []

    def _visit_function(self, node: ast.AsyncFunctionDef | ast.FunctionDef) -> None:
        self._stack.append(node.name)
        self.calls_by_function.setdefault(node.name, set())
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        if name is not None and self._stack:
            self.calls_by_function[self._stack[-1]].add(name)
        self.generic_visit(node)


def _transitive_guest_touchers(calls_by_function: dict[str, set[str]]) -> frozenset[str]:
    """Every function name that touches the guest cookie, directly or **transitively through a call
    to another module-local function that does** — a fixpoint over `calls_by_function`, so a route
    handler that calls a helper, which calls a helper, which calls
    `resolve_or_start_guest_session`, is still caught at any depth. A direct-call-only version of
    this scan is exactly the gap `test_ac24_transfer_route_exception_set_is_exactly_the_copy_route`
    must not have: today every 2.2 router happens to call the guest-touching function directly from
    the route handler's own body, so that version would still pass — silently proving nothing about
    the one shape it cannot see."""
    touching = {
        name for name, calls in calls_by_function.items() if calls & _GUEST_TOUCHING_CALL_NAMES
    }
    changed = True
    while changed:
        changed = False
        for name, calls in calls_by_function.items():
            if name not in touching and calls & touching:
                touching.add(name)
                changed = True
    return frozenset(touching)


class _NameReferenceCollector(ast.NodeVisitor):
    """Every bare identifier and attribute-access name anywhere in a module — R-10's check reads
    this for `"COOKIE_NAME"` without caring whether it was an import, a call argument or an
    attribute access."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        self.names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.names.add(node.attr)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name)


@cache
def _parsed_router_module(module_name: str) -> ast.Module:
    module = sys.modules[module_name]
    return ast.parse(inspect.getsource(module))


def _guest_touching_functions(module_name: str) -> frozenset[str]:
    collector = _FunctionCallCollector()
    collector.visit(_parsed_router_module(module_name))
    return _transitive_guest_touchers(collector.calls_by_function)


def _route_touches_guest_cookie_by_direct_call(route: APIRoute) -> bool:
    """True iff the route's own endpoint function's body contains a direct call to
    `resolve_or_start_guest_session` or `read_guest_token` — the thing the dependency graph, which
    only sees `Depends(...)` parameters, cannot see at all."""
    endpoint = route.endpoint
    module_name = getattr(endpoint, "__module__", "")
    if not module_name.startswith(_ROUTERS_PACKAGE_PREFIX):
        return False
    return endpoint.__name__ in _guest_touching_functions(module_name)


def _router_module_names() -> list[str]:
    """Every `routers/*.py` module, discovered from the package rather than hand-listed — a new
    router file is picked up automatically, the same reason `_iter_api_routes` walks `app.routes`
    live instead of trusting a maintained list."""
    package_dir = Path(_routers_package.__file__).resolve().parent
    return [
        f"{_ROUTERS_PACKAGE_PREFIX}.{path.stem}"
        for path in sorted(package_dir.glob("*.py"))
        if path.stem != "__init__"
    ]


def test_the_transitive_closure_catches_a_helper_indirected_guest_touching_call() -> None:
    """The walker's own positive control (CLAUDE.md: a skeleton satisfies every absence assertion —
    pair it with a discriminating positive), for the *fix* rather than for the AST scan itself.

    A route handler that calls a module-local helper, which itself calls
    `resolve_or_start_guest_session`, must still be flagged — even though the handler's own body
    never mentions that name. A synthetic module (a plain string, parsed with `ast.parse`) isolates
    the graph-walking logic from anything in `routers/`, and proves the closure catches this shape at
    two levels of indirection, stops at an unrelated function, and only reports functions that are
    genuinely reachable."""
    source = textwrap.dedent(
        """
        def _innermost_helper():
            resolve_or_start_guest_session()

        def _helper():
            _innermost_helper()

        def route_handler():
            _helper()

        def unrelated():
            pass
        """
    )
    collector = _FunctionCallCollector()
    collector.visit(ast.parse(source))

    touching = _transitive_guest_touchers(collector.calls_by_function)

    assert "route_handler" in touching, (
        "a route handler two calls away from resolve_or_start_guest_session must be caught "
        f"transitively — found: {sorted(touching)}"
    )
    assert "_helper" in touching
    assert "_innermost_helper" in touching
    assert "unrelated" not in touching


def test_ac32_transfer_route_exception_set_is_exactly_the_claim_route(
    app: FastAPI,
) -> None:
    """AC-24 (2.2), narrowed by AC-32 (2.4: the copy route is retired, the claim is the one
    transfer route left). The set of routes that depend on `require_user` **and** touch `tc_guest` —
    via `require_guest_session`/`resolve_or_start_guest_session` in the dependency graph, **or** a
    direct call the AST scan finds — must be exactly `{POST /api/me/guest-work/claim}`.
    A route added with both, by either mechanism, turns this red."""
    api_routes = list(_iter_api_routes(app.routes))
    assert len(api_routes) > 5, "the walker found suspiciously few routes — is it even recursing?"

    violations: set[str] = set()
    for route in api_routes:
        calls = _all_dependency_calls(route.dependant)
        touches_via_graph = (
            require_guest_session in calls or resolve_or_start_guest_session in calls
        )
        touches_via_ast = _route_touches_guest_cookie_by_direct_call(route)
        if require_user in calls and (touches_via_graph or touches_via_ast):
            for method in sorted(route.methods or ()):
                violations.add(f"{method} {route.path}")

    assert violations == {"POST /api/me/guest-work/claim"}, (
        f"the set of routes reading both credentials must be exactly the named transfer route "
        f"(ADR-0008 (f)); found: {sorted(violations)}"
    )


def test_ac24_r10_the_guest_cookie_name_is_referenced_only_inside_guest_session_py() -> None:
    """R-10: a *new* reader of `tc_guest` must pass through `read_guest_token` or
    `resolve_or_start_guest_session` (both in `guest_session.py`/`deps.py`) rather than importing
    `COOKIE_NAME` and reading the cookie jar by hand somewhere the walker above cannot see at all —
    a raw `request.cookies.get("tc_guest")` would touch no scanned helper and slip past both checks
    above. Scanned by the identifier, not the literal `"tc_guest"` string: several router
    docstrings mention that string in prose, and prose is not a reader."""
    offending: dict[str, set[str]] = {}
    for module_name in [*_router_module_names(), "tailorcraft.infrastructure.api.deps"]:
        collector = _NameReferenceCollector()
        collector.visit(_parsed_router_module(module_name))
        if "COOKIE_NAME" in collector.names:
            offending[module_name] = collector.names

    assert offending == {}, (
        f"COOKIE_NAME (the tc_guest constant) must be referenced only inside guest_session.py's "
        f"own read_guest_token/set_guest_cookie — found a direct reference in: {sorted(offending)}"
    )


def test_ac32_the_ast_scan_actually_finds_the_claim_routes_direct_calls() -> None:
    """AC-32's second positive control (slice 2.4): the claim is a transfer route read in the other
    direction — the bearer is a dependency, the cookie is read and cleared **in the body**. The scan
    must be able to see that body, or the exception-set test above is satisfied by a scan that cannot
    see the new route at all (the skeleton calls nothing, so it is invisible until T22)."""
    touching = _guest_touching_functions("tailorcraft.infrastructure.api.routers.me_guest_work")
    assert "claim_guest_work" in touching, (
        f"the AST scan found no guest-cookie call in routers/me_guest_work.py's claim_guest_work — "
        f"it found: {touching!r}. The handler must call read_guest_token / clear_guest_cookie "
        f"directly (ADR-0008 amendment (g): read in the body, never through a Depends)."
    )


# ---------------------------------------------------------------------------------------------
# AC-32 (slice 2.5, T27) — the `Origin` check's reach, and the three new routes' credentials.
#
# **The set is eight, not the spec's seven — and the difference is on purpose.** AC-32 reads "2.1's
# four plus `registration/confirm`, `password-reset`, `password-reset/confirm`", but `delete-account`
# (2.2) has carried `require_trusted_origin` since T18: it is a cookie-adjacent `POST` that signs
# nobody in and acts on a bearer, and the check is right there. Pinning seven would demand that 2.2's
# check be *removed* to go green. The pinned set is the seven AC-32 names **plus** `delete-account`;
# the spec row is amended to match (reported in T27's hand-back). A new `/api/auth` `POST` that
# forgets the check, or a route outside `/api/auth` that grows one, turns this red by name.
#
# The three new handlers are skeletons at T27 (they raise), so every absence below is satisfied by
# them already — which is why each is paired with a positive control over handlers that *do* touch
# the credential (`login`, `refresh`, `logout`) and over a route that does depend on `require_user`.
# ---------------------------------------------------------------------------------------------

_ORIGIN_CHECKED_POSTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/auth/register"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/refresh"),
        ("POST", "/api/auth/logout"),
        ("POST", "/api/auth/delete-account"),
        ("POST", "/api/auth/registration/confirm"),
        ("POST", "/api/auth/password-reset"),
        ("POST", "/api/auth/password-reset/confirm"),
    }
)
_NEW_2_5_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/auth/registration/confirm"),
        ("POST", "/api/auth/password-reset"),
        ("POST", "/api/auth/password-reset/confirm"),
    }
)
_REFRESH_COOKIE_CALL_NAMES = frozenset(
    {"read_refresh_token", "set_refresh_cookie", "clear_refresh_cookie", "mint_refresh_token"}
)


def _closure_over(
    calls_by_function: dict[str, set[str]], watched: frozenset[str]
) -> frozenset[str]:
    """Every function that calls a `watched` name directly or through module-local helpers —
    `_transitive_guest_touchers`' fixpoint, parametrised over the names."""
    touching = {name for name, calls in calls_by_function.items() if calls & watched}
    changed = True
    while changed:
        changed = False
        for name, calls in calls_by_function.items():
            if name not in touching and calls & touching:
                touching.add(name)
                changed = True
    return frozenset(touching)


def test_ac32_the_origin_checked_routes_are_exactly_the_eight_auth_posts(app: FastAPI) -> None:
    from tailorcraft.infrastructure.api.deps import require_trusted_origin

    checked = {
        (method, route.path)
        for route in _iter_api_routes(app.routes)
        if require_trusted_origin in _all_dependency_calls(route.dependant)
        for method in route.methods or ()
    }

    assert checked == _ORIGIN_CHECKED_POSTS, (
        f"missing: {sorted(_ORIGIN_CHECKED_POSTS - checked)}; "
        f"unexpected: {sorted(checked - _ORIGIN_CHECKED_POSTS)}"
    )


def test_ac32_none_of_the_three_new_routes_takes_a_bearer_a_guest_session_or_a_cookie(
    app: FastAPI,
) -> None:
    routes = {
        (method, route.path): route
        for route in _iter_api_routes(app.routes)
        for method in route.methods or ()
    }
    module_name = "tailorcraft.infrastructure.api.routers.auth"
    collector = _FunctionCallCollector()
    collector.visit(_parsed_router_module(module_name))
    guest_touchers = _closure_over(collector.calls_by_function, _GUEST_TOUCHING_CALL_NAMES)
    refresh_touchers = _closure_over(collector.calls_by_function, _REFRESH_COOKIE_CALL_NAMES)

    # Positive controls: the scan can see what it is asked to prove absent.
    assert require_user in _all_dependency_calls(routes[("GET", ME_URL)].dependant)
    assert {"login", "refresh", "logout"} <= refresh_touchers, sorted(refresh_touchers)

    for key in sorted(_NEW_2_5_ROUTES):
        route = routes[key]
        calls = _all_dependency_calls(route.dependant)
        assert require_user not in calls, key
        assert require_guest_session not in calls, key
        assert resolve_or_start_guest_session not in calls, key
        assert route.endpoint.__name__ not in guest_touchers, key
        assert route.endpoint.__name__ not in refresh_touchers, key
