"""Slice 4.3, T8 (test-after): AC-30 (a)-(d) and AC-31, the structural guarantees of the admin content
surface.

Reuses 4.1/4.2's walker and scans rather than writing a second one; each scan has a positive control,
so a pass cannot be a walker that finds nothing.

**On `require_admin`'s two homes.** Every content handler also takes `actor: AdminDep`, so deleting the
router-level `dependencies=[Depends(require_admin)]` leaves the walker green (the same dependency is
still in each route's graph): that mutation is equivalent for (a)'s walk. (a) therefore also pins the
router object itself, which is the only check that notices its removal.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from fastapi import Depends, FastAPI

from tailorcraft.infrastructure.api.deps import require_admin
from tailorcraft.infrastructure.api.routers import admin_content
from tests.api.test_admin_users_structure import _admin_routes, _mentions_429
from tests.api.test_auth_route_dependency_boundary import (
    _SRC,
    _all_dependency_calls,
    _unguarded_admin_routes,
)

_ROUTERS = _SRC / "infrastructure" / "api" / "routers"
_CONTENT_ROUTER = _ROUTERS / "admin_content.py"


def _content_routes(app: FastAPI) -> dict[tuple[str, str], object]:
    """The routes `admin_content.py` defines (the account actions share the prefix)."""
    return {
        k: v
        for k, v in _admin_routes(app).items()
        if Path(v.endpoint.__code__.co_filename).name == "admin_content.py"  # type: ignore[attr-defined]
    }


# --- (a) --------------------------------------------------------------------------------------


def test_ac30a_thirty_one_admin_routes_eleven_of_them_content_each_behind_require_admin(
    app: FastAPI,
) -> None:
    routes = _admin_routes(app)
    content = _content_routes(app)

    assert len(routes) == 31
    assert len(content) == 11
    assert _unguarded_admin_routes(app) == []
    for key, route in content.items():
        assert require_admin in _all_dependency_calls(route.dependant), key  # type: ignore[attr-defined]


def test_ac30a_the_content_router_itself_carries_require_admin() -> None:
    assert [d.dependency for d in admin_content.router.dependencies] == [require_admin]
    assert all(
        isinstance(d, type(Depends(require_admin))) for d in admin_content.router.dependencies
    )


# --- (b) --------------------------------------------------------------------------------------

_FORBIDDEN_SQLALCHEMY = {"Table", "delete", "text", "insert", "update", "select"}


def _forbidden_imports(tree: ast.Module) -> list[str]:
    """SQL building from `sqlalchemy`, any `persistence` module, and any `*Repository`/`*_table`."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "persistence" in module or "repositories" in module:
                found.append(f"imports from {module} at line {node.lineno}")
            for alias in node.names:
                if module.split(".")[0] == "sqlalchemy" and alias.name in _FORBIDDEN_SQLALCHEMY:
                    found.append(f"imports sqlalchemy.{alias.name} at line {node.lineno}")
                if alias.name.endswith(("Repository", "_table")):
                    found.append(f"imports {alias.name} at line {node.lineno}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "persistence" in alias.name or "repositories" in alias.name:
                    found.append(f"imports {alias.name} at line {node.lineno}")
    return found


def test_ac30b_the_content_router_imports_only_use_case_providers_no_sql_no_persistence() -> None:
    assert _forbidden_imports(ast.parse(_CONTENT_ROUTER.read_text())) == []


def test_ac30b_the_import_scan_has_a_positive_control() -> None:
    bad = (
        "from sqlalchemy import Table, delete, text\n"
        "from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table\n"
        "from tailorcraft.infrastructure.persistence.repositories.x import SqlAlchemyYRepository\n"
        "import tailorcraft.infrastructure.persistence.database\n"
    )
    assert len(_forbidden_imports(ast.parse(bad))) == 8, _forbidden_imports(ast.parse(bad))
    ok = "from tailorcraft.infrastructure.api.deps import DeleteSavedBaseCvDep, SessionDep\n"
    assert _forbidden_imports(ast.parse(ok)) == []


# --- (c) --------------------------------------------------------------------------------------


def test_ac30c_the_content_router_defines_no_port() -> None:
    tree = ast.parse(_CONTENT_ROUTER.read_text())
    ports = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and any(getattr(b, "id", getattr(b, "attr", "")) == "Protocol" for b in node.bases)
    ]
    assert ports == [], "no new Protocol: the port-binding test stays unchanged"


# --- (d) --------------------------------------------------------------------------------------


def test_ac30d_the_content_router_raises_no_429() -> None:
    assert _mentions_429(ast.parse(_CONTENT_ROUTER.read_text())) == []


# --- AC-31 ------------------------------------------------------------------------------------

_ALLOWED_PATH_OWNER_MODULES = {"admin_content.py", "admin_users.py"}


def _path_parameter_names(tree: ast.Module) -> set[str]:
    """Names in `{...}` of a route decorator's path that the decorated function takes."""
    names: set[str] = set()
    prefix_params: set[str] = set()
    for node in ast.walk(tree):  # `APIRouter(prefix="/…/{user_id}")` declares path parameters too
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    prefix_params |= set(re.findall(r"{(\w+)}", str(kw.value.value)))
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        args = {a.arg for a in [*fn.args.args, *fn.args.kwonlyargs, *fn.args.posonlyargs]}
        for deco in fn.decorator_list:
            if isinstance(deco, ast.Call) and deco.args and isinstance(deco.args[0], ast.Constant):
                path = str(deco.args[0].value)
                names |= {a for a in args if "{" + a + "}" in path}
        names |= args & prefix_params
    return names


def _bare_names(node: ast.expr) -> set[str]:
    """Names under `node`, skipping the base of an attribute access (`user.id` reads `id`)."""
    if isinstance(node, ast.Attribute):
        return set()
    if isinstance(node, ast.Name):
        return {node.id}
    return {
        n
        for child in ast.iter_child_nodes(node)
        if isinstance(child, ast.expr)
        for n in _bare_names(child)
    }


def _path_built_owners(tree: ast.Module) -> list[tuple[int, str]]:
    """`UserOwner(...)` / `UserId(...)` built from a **path parameter** of one of the module's routes
    (a credential's id is read off an object, `user.id`, or arrives under a name no path declares)."""
    path_names = _path_parameter_names(tree)
    return sorted(
        {
            (call.lineno, call.func.id)
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id in {"UserOwner", "UserId"}
            and any(_bare_names(a) & path_names for a in call.args)
        }
    )


def test_ac31_a_path_built_owner_appears_only_in_the_two_admin_routers() -> None:
    offenders = {
        path.name: hits
        for path in sorted(_ROUTERS.glob("*.py"))
        if path.name not in _ALLOWED_PATH_OWNER_MODULES
        and (hits := _path_built_owners(ast.parse(path.read_text())))
    }
    assert offenders == {}


def test_ac31_the_scan_finds_the_admin_content_routers_own_owner() -> None:
    """Positive control on the real tree: the allowed module is flagged by the very scan."""
    assert _path_built_owners(ast.parse(_CONTENT_ROUTER.read_text()))


def test_ac31_a_planted_module_with_a_path_built_owner_is_caught_and_a_credential_one_is_not() -> (
    None
):
    planted = "@router.get('/x/{uid}')\nasync def f(uid):\n    return UserOwner(UserId(uid))\n"
    credential = "async def g(user):\n    return UserOwner(user.id)\n"

    assert _path_built_owners(ast.parse(planted)) == [(3, "UserId"), (3, "UserOwner")]
    assert _path_built_owners(ast.parse(credential)) == []


def test_ac31_every_route_in_the_two_modules_carries_require_admin(app: FastAPI) -> None:
    checked = 0
    for key, route in _admin_routes(app).items():
        module = Path(route.endpoint.__code__.co_filename).name  # type: ignore[attr-defined]
        if module in _ALLOWED_PATH_OWNER_MODULES:
            checked += 1
            assert require_admin in _all_dependency_calls(route.dependant), key  # type: ignore[attr-defined]
    assert checked >= 16, "the scan must reach both modules' routes (6 users + 11 content, minus 1)"
