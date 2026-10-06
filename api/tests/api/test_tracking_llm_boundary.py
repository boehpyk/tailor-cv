"""AC-41 — the LLM and tailoring are untouched by slice 3.1 (T22).

The spec's clause has three parts:

1. `git diff main --stat -- infrastructure/llm domain/tailoring application/tailoring` is **empty**.
   That cannot run here — `/app` is `./api` with no `.git` — so it stays a `/verify` gate, as 2.1's
   AC-52, 2.2's AC-58 and 2.3's AC-54 did. It is **not** asserted by a test that skips.
2. `inspect.signature(LlmPort.tailor)` is `(self, cv: ExtractedText, posting: JobPostingText)`.
3. **No tracking code path imports `LlmPort`, a queue port or `infrastructure/tasks`** — an AST scan
   over `application/tracking` and the router (extended here, on purpose, to the rest of the
   slice's own modules: the domain, the schemas, the repository, the board query and the mapping —
   every file 3.1 added), **plus** a dependency-graph walk of the five routes, because an import
   check cannot see a `Depends(get_tailoring_queue)` reached through `deps.py`.

All three pass on arrival against the T20 skeleton — that is the point: they are the regression
guard for T23's GREEN, whose temptation is a "refresh the run" call into tailoring. Each is paired
with a **positive control** so the scan cannot pass by scanning nothing: the scanner is shown a
synthetic module that does import the forbidden names, and must flag every one; the walker is shown
`POST /api/me/tailoring-runs`, which really does depend on the tailoring queue, and must see it.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from fastapi import FastAPI

import tailorcraft
from tailorcraft.domain.intake.value_objects import ExtractedText
from tailorcraft.domain.posting.value_objects import JobPostingText
from tailorcraft.domain.tailoring.ports import LlmPort
from tailorcraft.infrastructure.api.deps import (
    get_account_mail_queue,
    get_celery,
    get_export_queue,
    get_llm,
    get_tailoring_queue,
)
from tests.api.test_auth_route_dependency_boundary import _all_dependency_calls, _iter_api_routes

_SRC = Path(tailorcraft.__file__).parent

# Every module the slice added or owns, relative to `src/tailorcraft`. A glob, not a list, for the
# directories: a file added at T23 is scanned without anyone remembering to add it.
_SCANNED = [
    *sorted((_SRC / "application" / "tracking").rglob("*.py")),
    *sorted((_SRC / "domain" / "tracking").rglob("*.py")),
    *sorted((_SRC / "infrastructure" / "persistence" / "repositories" / "tracking").rglob("*.py")),
    *sorted((_SRC / "infrastructure" / "persistence" / "mapping" / "tracking").rglob("*.py")),
    _SRC / "infrastructure" / "persistence" / "queries" / "application_board.py",
    _SRC / "infrastructure" / "api" / "routers" / "me_tracked_applications.py",
    _SRC / "infrastructure" / "api" / "schemas" / "tracking.py",
]

# Imported module prefixes that mean "the LLM, a queue or the worker".
_FORBIDDEN_MODULE_PREFIXES = (
    "tailorcraft.infrastructure.llm",
    "tailorcraft.infrastructure.tasks",
    "tailorcraft.infrastructure.tailoring.queue",
    "tailorcraft.infrastructure.export.queue",
    "celery",
    "kombu",
    "google",
)
# Names that mean the same thing wherever they appear — imported, aliased, or reached as `x.Name`.
_FORBIDDEN_NAMES = frozenset(
    {
        "LlmPort",
        "GeminiLlm",
        "get_llm",
        "TailoringQueuePort",
        "ExportQueuePort",
        "AccountMailQueuePort",
        "get_tailoring_queue",
        "get_export_queue",
        "get_account_mail_queue",
        "get_celery",
        "CeleryDep",
        "ExecuteTailoringRun",
        "RequestTailoringRun",
    }
)


def _offences(source: str) -> set[str]:
    """What `source` imports or names that the slice may never touch (function-local imports and
    attribute access included)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(_FORBIDDEN_MODULE_PREFIXES):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module.startswith(_FORBIDDEN_MODULE_PREFIXES):
                found.add(node.module)
            found.update(a.name for a in node.names if a.name in _FORBIDDEN_NAMES)
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            found.add(node.attr)
    return found


def test_ac41_the_llm_port_signature_is_pinned() -> None:
    signature = inspect.signature(LlmPort.tailor)

    assert list(signature.parameters) == ["self", "cv", "posting"]
    assert signature.parameters["cv"].annotation in ("ExtractedText", ExtractedText)
    assert signature.parameters["posting"].annotation in ("JobPostingText", JobPostingText)


def test_ac41_the_scan_covers_every_module_the_slice_owns() -> None:
    """The scan's own precondition: the file list is not empty and holds the pieces the spec names."""
    names = {path.name for path in _SCANNED}

    assert {
        "track_application.py",
        "move_tracked_application.py",
        "retitle_tracked_application.py",
        "untrack_application.py",
        "show_application_board.py",
        "me_tracked_applications.py",
        "tracking.py",
        "tracked_application.py",
        "application_board.py",
    } <= names, sorted(names)
    assert all(path.exists() for path in _SCANNED)


def test_ac41_the_scanner_flags_every_forbidden_import_it_is_shown() -> None:
    """The positive control: a module that does everything the slice must never do."""
    source = """
from tailorcraft.domain.tailoring.ports import LlmPort, TailoringQueuePort
import tailorcraft.infrastructure.tasks.app
from tailorcraft.infrastructure.llm.gemini import GeminiLlm
from tailorcraft.infrastructure.api import deps

def f(celery):
    from celery import Celery
    queue = deps.get_export_queue
    return deps.get_llm
"""

    assert _offences(source) >= {
        "LlmPort",
        "TailoringQueuePort",
        "tailorcraft.infrastructure.tasks.app",
        "tailorcraft.infrastructure.llm.gemini",
        "GeminiLlm",
        "celery",
        "get_export_queue",
        "get_llm",
    }


def test_ac41_the_scanner_ignores_an_innocent_module() -> None:
    source = "from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRun\n"

    assert _offences(source) == set()


@pytest.mark.parametrize("path", _SCANNED, ids=lambda p: str(p.relative_to(_SRC)))
def test_ac41_no_tracking_module_imports_the_llm_a_queue_or_the_worker(path: Path) -> None:
    assert _offences(path.read_text()) == set(), path


_TRACKING_PATHS = {
    ("GET", "/api/me/board"),
    ("POST", "/api/me/tracked-applications"),
    ("PUT", "/api/me/tracked-applications/{tracked_application_id}/stage"),
    ("PUT", "/api/me/tracked-applications/{tracked_application_id}/title"),
    ("DELETE", "/api/me/tracked-applications/{tracked_application_id}"),
}
_FORBIDDEN_DEPENDENCIES = {
    get_llm,
    get_tailoring_queue,
    get_export_queue,
    get_account_mail_queue,
    get_celery,
}


def test_ac41_no_tracking_route_depends_on_the_llm_a_queue_or_celery(app: FastAPI) -> None:
    seen: set[tuple[str, str]] = set()
    for route in _iter_api_routes(app.routes):
        for method in route.methods or ():
            if (method, route.path) in _TRACKING_PATHS:
                seen.add((method, route.path))
                reached = _all_dependency_calls(route.dependant) & _FORBIDDEN_DEPENDENCIES
                assert reached == set(), (
                    method,
                    route.path,
                    sorted(getattr(c, "__name__", repr(c)) for c in reached),
                )

    assert seen == _TRACKING_PATHS, "the walker must find all five routes, or it proved nothing"


def test_ac41_the_dependency_walk_does_see_a_queue_when_there_is_one(app: FastAPI) -> None:
    """The positive control for the walk: `POST /api/me/tailoring-runs` really does enqueue."""
    routes = {
        (method, route.path): route
        for route in _iter_api_routes(app.routes)
        for method in route.methods or ()
    }

    reached = _all_dependency_calls(routes[("POST", "/api/me/tailoring-runs")].dependant)

    assert get_tailoring_queue in reached
