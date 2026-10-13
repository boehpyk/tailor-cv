"""Slice 4.2, T16 (test-after): AC-28 (a)-(f), the structural guarantees of the admin users surface.

Reuses 4.1's walker and scans (`test_auth_route_dependency_boundary.py`) rather than writing a second
one. Each scan has a positive control, so a pass cannot be a walker that finds nothing.

(c) the transfer-route set and (d) no 429 are stated here by calling the existing scans' own
checks, so this slice owns the claim even though the scans are 2.2/3.3's.
"""

from __future__ import annotations

import ast

from fastapi import FastAPI

from tailorcraft.infrastructure.api.deps import get_erase_account, require_admin
from tests.api.test_auth_route_dependency_boundary import (
    _SRC,
    _all_dependency_calls,
    _is_admin_path,
    _iter_api_routes,
    _unguarded_admin_routes,
)
from tests.integration.test_port_bindings import _BOUND_ELSEWHERE

_ROUTERS = _SRC / "infrastructure" / "api" / "routers"
_ADMIN_ROUTER_FILES = sorted(_ROUTERS.glob("admin*.py"))
_USER_AGGREGATE = _SRC / "domain" / "identity" / "user.py"
_EXPECTED_ADMIN_ROUTES = {
    ("GET", "/api/admin/access"),
    ("GET", "/api/admin/users"),
    ("GET", "/api/admin/users/{user_id}"),
    ("POST", "/api/admin/users/{user_id}/disable"),
    ("POST", "/api/admin/users/{user_id}/enable"),
    ("DELETE", "/api/admin/users/{user_id}"),
    ("GET", "/api/admin/users/{user_id}/base-cvs"),
    ("GET", "/api/admin/users/{user_id}/base-cvs/{base_cv_id}"),
    ("DELETE", "/api/admin/users/{user_id}/base-cvs/{base_cv_id}"),
    ("GET", "/api/admin/users/{user_id}/job-postings"),
    ("GET", "/api/admin/users/{user_id}/job-postings/{job_posting_id}"),
    ("GET", "/api/admin/users/{user_id}/tailoring-runs"),
    ("GET", "/api/admin/users/{user_id}/tailoring-runs/{tailoring_run_id}"),
    ("DELETE", "/api/admin/users/{user_id}/tailoring-runs/{tailoring_run_id}"),
    (
        "GET",
        "/api/admin/users/{user_id}/tailoring-runs/{tailoring_run_id}/documents/{kind}/download",
    ),
    ("GET", "/api/admin/users/{user_id}/tailoring-runs/{tailoring_run_id}/exports"),
    ("GET", "/api/admin/users/{user_id}/export-jobs/{export_job_id}/file"),
    ("GET", "/api/admin/guest-sessions"),
    ("GET", "/api/admin/guest-sessions/{session_id}"),
    ("DELETE", "/api/admin/guest-sessions/{session_id}"),
    ("GET", "/api/admin/guest-sessions/{session_id}/base-cvs/{base_cv_id}"),
    ("GET", "/api/admin/guest-sessions/{session_id}/job-postings/{job_posting_id}"),
    ("GET", "/api/admin/guest-sessions/{session_id}/tailoring-runs/{tailoring_run_id}"),
    (
        "GET",
        "/api/admin/guest-sessions/{session_id}/tailoring-runs/{tailoring_run_id}/documents/{kind}/download",
    ),
    ("GET", "/api/admin/guest-sessions/{session_id}/tailoring-runs/{tailoring_run_id}/exports"),
    ("GET", "/api/admin/guest-sessions/{session_id}/export-jobs/{export_job_id}/file"),
    ("GET", "/api/admin/storage"),
    ("GET", "/api/admin/guest-purge"),
    ("POST", "/api/admin/guest-purge"),
    ("GET", "/api/admin/orphan-sweep"),
    ("POST", "/api/admin/orphan-sweep"),
}


def _admin_routes(app: FastAPI) -> dict[tuple[str, str], object]:
    return {
        (method, route.path): route
        for route in _iter_api_routes(app.routes)
        if _is_admin_path(route.path)
        for method in route.methods or ()
    }


# --- (a) --------------------------------------------------------------------------------------


def test_ac28a_the_admin_surface_is_exactly_thirty_one_routes_each_behind_require_admin(
    app: FastAPI,
) -> None:
    routes = _admin_routes(app)

    assert set(routes) == _EXPECTED_ADMIN_ROUTES
    assert _unguarded_admin_routes(app) == []
    for key, route in routes.items():
        assert require_admin in _all_dependency_calls(route.dependant), key  # type: ignore[attr-defined]


# --- (b) --------------------------------------------------------------------------------------


def _writes_sql_or_imports_a_table(tree: ast.Module) -> list[str]:
    """`delete(...)`/`.delete(` calls, a string holding `DELETE`, and an import of a `Table` (a
    `*_table` name or `sqlalchemy.Table`)."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "delete" and isinstance(func, ast.Name):
                found.append(f"delete() at line {node.lineno}")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "delete from" in " ".join(node.value.lower().split())
        ):
            found.append(f"DELETE SQL at line {node.lineno}")
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name.endswith("_table") or alias.name == "Table":
                    found.append(f"imports {alias.name} at line {node.lineno}")
    return found


def test_ac28b_no_admin_router_module_deletes_with_sql_or_imports_a_table() -> None:
    assert _ADMIN_ROUTER_FILES, "the scan found no routers/admin*.py"
    offenders = {
        path.name: hits
        for path in _ADMIN_ROUTER_FILES
        if (hits := _writes_sql_or_imports_a_table(ast.parse(path.read_text())))
    }
    assert offenders == {}


def test_ac28b_the_sql_scan_has_a_positive_control() -> None:
    bad = (
        "from sqlalchemy import Table, delete\n"
        "from x import user_table\n"
        "q = delete(t)\n"
        "r = 'DELETE  FROM t'\n"
    )
    hits = _writes_sql_or_imports_a_table(ast.parse(bad))
    assert len(hits) == 4, hits
    assert _writes_sql_or_imports_a_table(ast.parse("x = 1\nsession.commit()")) == []


def test_ac28b_the_erase_route_resolves_erase_account_from_delete_accounts_provider(
    app: FastAPI,
) -> None:
    routes = _admin_routes(app)
    erase = routes[("DELETE", "/api/admin/users/{user_id}")]
    own = _all_dependency_calls(erase.dependant)  # type: ignore[attr-defined]
    delete_account = next(
        r
        for r in _iter_api_routes(app.routes)
        if r.path == "/api/auth/delete-account" and "POST" in (r.methods or ())
    )
    theirs = _all_dependency_calls(delete_account.dependant)

    assert get_erase_account in own
    assert get_erase_account in theirs


# --- (c) --------------------------------------------------------------------------------------


def test_ac28c_no_admin_route_touches_the_guest_cookie(app: FastAPI) -> None:
    from tailorcraft.infrastructure.api.deps import (
        require_guest_session,
        resolve_or_start_guest_session,
    )
    from tests.api.test_auth_route_dependency_boundary import (
        _route_touches_guest_cookie_by_direct_call,
    )

    for key, route in _admin_routes(app).items():
        calls = _all_dependency_calls(route.dependant)  # type: ignore[attr-defined]
        assert require_guest_session not in calls, key
        assert resolve_or_start_guest_session not in calls, key
        assert not _route_touches_guest_cookie_by_direct_call(route), key  # type: ignore[arg-type]


# --- (d) --------------------------------------------------------------------------------------


def _mentions_429(tree: ast.Module) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == 429:
            lines.append(node.lineno)
        if isinstance(node, ast.Attribute) and node.attr == "HTTP_429_TOO_MANY_REQUESTS":
            lines.append(node.lineno)
        if isinstance(node, ast.Name) and node.id in {"HTTP_429_TOO_MANY_REQUESTS", "RateLimited"}:
            lines.append(node.lineno)
    return lines


def test_ac28d_no_admin_router_module_raises_a_429() -> None:
    assert _ADMIN_ROUTER_FILES
    offenders = {
        path.name: lines
        for path in _ADMIN_ROUTER_FILES
        if (lines := _mentions_429(ast.parse(path.read_text())))
    }
    assert offenders == {}


def test_ac28d_the_429_scan_has_a_positive_control() -> None:
    assert _mentions_429(
        ast.parse("raise HTTPException(429)\nx = status.HTTP_429_TOO_MANY_REQUESTS")
    )
    assert _mentions_429(ast.parse("x = 1")) == []


# --- (e) --------------------------------------------------------------------------------------


def test_ac28e_the_user_directory_query_is_bound_not_exempted() -> None:
    assert "UserDirectoryQuery" not in _BOUND_ELSEWHERE, "T2's temporary exemption was left behind"


# --- (f) --------------------------------------------------------------------------------------

_FIELDS = {"disabled_at", "_disabled_at"}


def _disabled_writes(tree: ast.Module) -> list[tuple[int, str]]:
    """Attribute assignments, `setattr`, Core `.values(...)` and `SET disabled_at` SQL."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Attribute) and t.attr in _FIELDS:
                found.append((t.lineno, "attribute"))
        if isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "setattr"
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _FIELDS
            ):
                found.append((node.lineno, "setattr"))
            if isinstance(node.func, ast.Attribute) and node.func.attr == "values":
                if any(k.arg in _FIELDS for k in node.keywords):
                    found.append((node.lineno, "values"))
                for a in node.args:
                    if isinstance(a, ast.Dict) and any(
                        isinstance(k, ast.Constant) and k.value in _FIELDS for k in a.keys
                    ):
                        found.append((node.lineno, "values-dict"))
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "set disabled_at" in " ".join(node.value.lower().split())
        ):
            found.append((node.lineno, "sql"))
    return found


def test_ac28f_nothing_outside_the_aggregate_writes_disabled_at() -> None:
    offenders = {
        str(path.relative_to(_SRC)): writes
        for path in sorted(_SRC.rglob("*.py"))
        if path != _USER_AGGREGATE and (writes := _disabled_writes(ast.parse(path.read_text())))
    }
    assert offenders == {}, f"disabled_at written outside the aggregate: {offenders}"


def test_ac28f_inside_the_aggregate_only_register_disable_and_enable_assign_it() -> None:
    tree = ast.parse(_USER_AGGREGATE.read_text())
    assigning = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        and _disabled_writes(ast.Module(body=fn.body, type_ignores=[]))
    }
    assert assigning == {"register_with_password", "disable", "enable"}


def test_ac28f_the_scan_has_a_positive_control() -> None:
    bad = (
        "user._disabled_at = x\nsetattr(u, 'disabled_at', 1)\n"
        "q = 'UPDATE t SET  disabled_at = now()'\nupdate(t).values(disabled_at=x)\n"
        "update(t).values({'disabled_at': x})\n"
    )
    kinds = sorted(kind for _, kind in _disabled_writes(ast.parse(bad)))
    assert kinds == ["attribute", "setattr", "sql", "values", "values-dict"]
    assert _disabled_writes(ast.parse("x = 1\nupdate(t).values(user_id=u)")) == []


def test_ac28f_no_router_module_names_an_account_writer_but_the_two_use_cases() -> None:
    """The routers reach `disabled_at` only through `DisableAccount` / `EnableAccount`."""
    for path in sorted(_ROUTERS.glob("*.py")):
        tree = ast.parse(path.read_text())
        calls = {
            n.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr in {"disable", "enable"}
        }
        assert calls == set(), f"{path.name} calls the aggregate's {sorted(calls)} directly"
