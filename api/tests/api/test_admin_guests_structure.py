"""Slice 4.4, T16 (test-after): AC-27 (b)-(e), the structural guarantees of the admin guest and
retention routers. (a) is `test_admin_users_structure`'s thirty-one-route walk (`_EXPECTED_ADMIN_ROUTES`
lists the fourteen new routes) and (f) its 429 scan over every `routers/admin*.py`; both already
cover the new modules, and the (a) count is asserted again here so this slice owns the claim.

(c) is 4.3's acting-as scan, widened in `test_admin_content_structure.py` (a test edit of an existing
structural test, allowed by AC-27 (c)): `GuestOwner(...)` built from a path parameter is permitted
**only** in `admin_guests.py`, and a user owner only in the two user routers.
"""

from __future__ import annotations

import ast

from fastapi import Depends, FastAPI

from tailorcraft.infrastructure.api.deps import require_admin
from tailorcraft.infrastructure.api.routers import (
    admin_content,
    admin_guests,
    admin_retention,
    admin_users,
)
from tests.api.test_admin_content_structure import _ROUTERS, _forbidden_imports
from tests.api.test_admin_users_structure import _admin_routes
from tests.api.test_auth_route_dependency_boundary import _unguarded_admin_routes
from tests.integration.test_port_bindings import _BOUND_ELSEWHERE


def test_ac27a_the_admin_surface_is_thirty_one_routes_each_behind_require_admin(
    app: FastAPI,
) -> None:
    assert len(_admin_routes(app)) == 31
    assert _unguarded_admin_routes(app) == []


def test_ac27b_all_four_admin_routers_carry_require_admin_on_the_router_itself() -> None:
    """Every handler also takes `actor: AdminDep`, so removing the router-level dependency changes no
    dependency graph (4.3's equivalent mutant); only the router object notices."""
    for module in (admin_guests, admin_retention, admin_users, admin_content):
        deps = module.router.dependencies
        assert [d.dependency for d in deps] == [require_admin], module.__name__
        assert all(isinstance(d, type(Depends(require_admin))) for d in deps), module.__name__


def test_ac27d_the_guest_session_directory_query_is_bound_not_exempted() -> None:
    assert "GuestSessionDirectoryQuery" not in _BOUND_ELSEWHERE


def test_ac27e_neither_new_router_imports_a_table_a_repository_or_sqlalchemy_dml() -> None:
    for name in ("admin_guests.py", "admin_retention.py"):
        # `admin_guests.py` names the *domain* `GuestSessionRepository` Protocol as a parameter type
        # (the live-session helper); the concrete repository is reached only through its provider.
        hits = [
            h
            for h in _forbidden_imports(ast.parse((_ROUTERS / name).read_text()))
            if not h.startswith("imports GuestSessionRepository at")
        ]
        assert hits == [], (name, hits)
