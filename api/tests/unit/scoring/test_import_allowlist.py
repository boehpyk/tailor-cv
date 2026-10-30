"""`domain/scoring/` imports only the standard library, `domain.shared` and `domain.identity`
(ADR-0040 decision 1). An allow-list, so a context named tomorrow is refused without anyone updating
this. It walks each module's own AST, function-local imports included.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import tailorcraft.domain.scoring as scoring

SCORING_ROOT = Path(scoring.__file__).parent

_ALLOWED_DOMAIN_PACKAGES = frozenset({"shared", "identity", "scoring"})


def _modules() -> list[Path]:
    return sorted(SCORING_ROOT.rglob("*.py"))


def _absolute_import_targets(source: str) -> set[str]:
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
        parts = target.split(".")
        if parts[0] == "tailorcraft":
            if len(parts) < 3 or parts[1] != "domain" or parts[2] not in _ALLOWED_DOMAIN_PACKAGES:
                bad.add(target)
        elif parts[0] not in sys.stdlib_module_names:
            bad.add(target)
    return bad


def test_the_walk_covers_every_scoring_module() -> None:
    """Guard the guard: an empty walk would pass while checking nothing."""
    assert {"errors.py", "events.py", "fit_score.py", "value_objects.py"} <= {
        p.name for p in _modules()
    }


@pytest.mark.parametrize("module_path", _modules(), ids=lambda p: p.name)
def test_scoring_module_imports_only_stdlib_shared_and_identity(module_path: Path) -> None:
    offenders = _offenders(module_path.read_text())

    assert not offenders, (
        f"{module_path.name} imports {sorted(offenders)}. `domain/scoring` may import the standard "
        f"library, `domain.shared` and `domain.identity` only (ADR-0040 decision 1)."
    )


@pytest.mark.parametrize(
    "line",
    [
        "from tailorcraft.domain.tailoring.value_objects import ModelName",
        "from tailorcraft.domain.intake.value_objects import ExtractedText",
        "from tailorcraft.domain.posting.value_objects import JobPostingText",
        "from tailorcraft.domain import tailoring",
        "import tailorcraft.application.scoring",
        "from tailorcraft.infrastructure.config import get_settings",
        "import pydantic",
    ],
)
def test_the_scanner_flags_a_forbidden_import(line: str) -> None:
    """Positive control: the scanner can see what it forbids, so a clean sweep above means clean."""
    assert _offenders(line)


@pytest.mark.parametrize(
    "line",
    [
        "from __future__ import annotations",
        "import hashlib",
        "from tailorcraft.domain.shared.events import DomainEvent",
        "from tailorcraft.domain.identity.ownership import Owner",
        "from tailorcraft.domain.scoring.errors import InvalidFitGap",
    ],
)
def test_the_scanner_accepts_an_allowed_import(line: str) -> None:
    assert not _offenders(line)
