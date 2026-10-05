"""AC-6's design assertion: `domain/tracking/` imports only the standard library, `domain.shared`
and `domain.identity` (for `UserId`) -- never a sibling context (ADR-0029 decision 1, the owner's T0
amendment of 2026-10-04), never a third party, never an outer layer.

Same shape as `tests/unit/identity/test_import_allowlist.py`, but an **allow-list** rather than a
forbidden list: a seventh context named tomorrow is refused without anyone remembering to add it.
It walks each module's own AST (function-local imports included).

This test passes on arrival against the skeleton; it is a regression guard for T5's GREEN, whose
temptation is `from tailorcraft.domain.tailoring... import TailoringRunStatus` for the
"succeeded only" rule (that rule is the use case's).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import tailorcraft.domain.tracking as tracking

TRACKING_ROOT = Path(tracking.__file__).parent

_ALLOWED_DOMAIN_PACKAGES = frozenset({"shared", "identity", "tracking"})


def _tracking_modules() -> list[Path]:
    return sorted(TRACKING_ROOT.rglob("*.py"))


def _absolute_import_targets(source: str) -> set[str]:
    """Every dotted module path an absolute import names, function-local ones included. For
    `from tailorcraft.domain import x`, `tailorcraft.domain.x` (the name is the package)."""
    targets: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "tailorcraft.domain":
                targets.update(f"tailorcraft.domain.{alias.name}" for alias in node.names)
            else:
                targets.add(node.module)
    return targets


def _offenders(source: str) -> set[str]:
    bad: set[str] = set()
    for target in _absolute_import_targets(source):
        top = target.split(".")[0]
        if top == "tailorcraft":
            parts = target.split(".")
            if len(parts) < 3 or parts[1] != "domain" or parts[2] not in _ALLOWED_DOMAIN_PACKAGES:
                bad.add(target)
        elif top not in sys.stdlib_module_names and top != "__future__":
            bad.add(target)
    return bad


def test_the_walk_covers_every_tracking_module() -> None:
    """Guard the guard: the parametrized test below must actually cover the real modules."""
    names = {p.name for p in _tracking_modules()}

    assert {
        "board.py",
        "errors.py",
        "events.py",
        "tracked_application.py",
        "value_objects.py",
    } <= names


@pytest.mark.parametrize("module_path", _tracking_modules(), ids=lambda p: p.name)
def test_tracking_module_imports_only_stdlib_shared_and_identity(module_path: Path) -> None:
    offenders = _offenders(module_path.read_text())

    assert not offenders, (
        f"{module_path.name} imports {sorted(offenders)}. `domain/tracking` may import the standard "
        f"library, `domain.shared` and `domain.identity` only (ADR-0029 decision 1)."
    )


@pytest.mark.parametrize(
    "line",
    [
        "from tailorcraft.domain.tailoring.value_objects import TailoringRunId",
        "from tailorcraft.domain.posting.value_objects import JobPostingId",
        "from tailorcraft.domain.intake.value_objects import BaseCvId",
        "from tailorcraft.domain.export.value_objects import ExportJobId",
        "from tailorcraft.domain.retention import policy",
        "from tailorcraft.domain import tailoring",
        "import tailorcraft.application.tracking",
        "from tailorcraft.infrastructure.config import get_settings",
        "import pydantic",
        "from sqlalchemy import select",
    ],
)
def test_the_scanner_flags_a_forbidden_import(line: str) -> None:
    """Positive control: the scanner can see what it forbids, so a clean sweep above means clean."""
    assert _offenders(line)


@pytest.mark.parametrize(
    "line",
    [
        "from __future__ import annotations",
        "from dataclasses import dataclass",
        "from tailorcraft.domain.shared.events import DomainEvent",
        "from tailorcraft.domain.identity.value_objects import UserId",
        "from tailorcraft.domain.tracking.errors import InvalidApplicationTitle",
    ],
)
def test_the_scanner_accepts_an_allowed_import(line: str) -> None:
    assert not _offenders(line)
