"""AC-25 — the two export twins share one body (slice 3.2, T18 RED).

`layout_template` is read from the request in exactly one place: `routers/_export_handlers.py`.
Neither `routers/export.py` (guest) nor `routers/me_tailoring_runs.py` (account) mentions it, and
both delegate the POST to the shared `request_export`. The transfer-route set (ADR-0008) is pinned
elsewhere (`test_auth_route_dependency_boundary.py`) and is re-asserted here only by reference.

Mixed state at T18: "no other router mentions it" and "both twins delegate" are green on arrival (the
skeleton added the field to the schema, not to a router); the **positive control** — the shared
handler reads `body.layout_template` — is red until T19 threads it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import tailorcraft.infrastructure.api.routers as routers_package

ROUTERS = Path(routers_package.__file__).parent
SHARED = "_export_handlers.py"


def _source(name: str) -> str:
    return (ROUTERS / name).read_text()


def _function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_the_shared_handler_reads_the_layout_from_the_request_body() -> None:
    """Positive control for the absence test below: the one place that may read it, does."""
    handler = _function(ast.parse(_source(SHARED)), "request_export")

    reads = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Attribute)
        and node.attr == "layout_template"
        and isinstance(node.value, ast.Name)
        and node.value.id == "body"
    ]

    assert reads, "request_export never reads body.layout_template"


def test_no_other_router_module_mentions_the_layout() -> None:
    others = sorted(p.name for p in ROUTERS.glob("*.py") if p.name != SHARED)
    assert {"export.py", "me_tailoring_runs.py"} <= set(others), "the scan must cover both twins"

    mentioning = [name for name in others if "layout_template" in _source(name)]

    assert mentioning == [], f"layout_template is read outside {SHARED}: {mentioning}"


@pytest.mark.parametrize(
    ("module", "alias"), [("export.py", "handlers"), ("me_tailoring_runs.py", "export_handlers")]
)
def test_both_twins_delegate_the_post_to_the_shared_handler(module: str, alias: str) -> None:
    tree = ast.parse(_source(module))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "request_export"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == alias
    ]

    assert len(calls) == 1, f"{module} must call {alias}.request_export exactly once"
