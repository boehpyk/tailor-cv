"""The default-deny backstop for the hexagonal boundary.

`lint-imports` enforces the layer rules with an *explicit* forbidden list — it only knows about the
packages someone thought to name. This test needs no list: it walks every import statement in
`tailorcraft.domain` and rejects anything that is not the standard library or the domain itself.

That difference matters. A dependency added six months from now by someone who never read
pyproject.toml is caught here automatically and would sail past the forbidden list. Default-deny is
the whole point of a boundary; an allow-list you have to remember to update is a boundary that
degrades every time you are busy.

Constitution §4 · ADR-0002.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import tailorcraft.domain

DOMAIN_ROOT = Path(tailorcraft.domain.__file__).parent
STDLIB = sys.stdlib_module_names


def _domain_modules() -> list[Path]:
    return sorted(DOMAIN_ROOT.rglob("*.py"))


def _imported_top_levels(source: str) -> set[str]:
    """Every top-level package name this module imports."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        # `node.level == 0` skips relative imports (`from .clock import Clock`), which have no
        # top-level module name to check and are by definition inside the domain already.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_the_domain_package_is_not_empty() -> None:
    """Guard the guard.

    Every assertion below iterates over a collection of files. If that collection were ever empty —
    a moved package, a renamed directory — every test in this module would pass while checking
    nothing at all. This is the assertion that cannot be satisfied by an absence.
    """
    assert len(_domain_modules()) >= 3


@pytest.mark.parametrize("module_path", _domain_modules(), ids=lambda p: p.name)
def test_domain_module_imports_only_the_standard_library(module_path: Path) -> None:
    """No third-party import may appear anywhere under `domain/`."""
    imported = _imported_top_levels(module_path.read_text())

    offenders = {
        name
        for name in imported
        if name not in STDLIB and name != "tailorcraft" and not name.startswith("_")
    }

    assert not offenders, (
        f"{module_path.relative_to(DOMAIN_ROOT.parent)} imports {sorted(offenders)}, which is not "
        f"the standard library. The domain layer depends on nothing external (Constitution §4). "
        f"Put a Protocol port here and the implementation in tailorcraft.infrastructure."
    )


def _imported_dotted_paths(source: str) -> set[str]:
    """Every module path this module imports, at full dotted length."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module_path", _domain_modules(), ids=lambda p: p.name)
def test_domain_module_does_not_import_other_layers(module_path: Path) -> None:
    """`domain` never imports `application` or `infrastructure` — dependencies point inward.

    Checked against the parsed imports rather than the raw text. The first version of this test did
    a substring search over the whole file and failed on a *docstring* that mentioned
    `tailorcraft.infrastructure.clock` by name — a false positive that would have taught exactly the
    wrong lesson: that documenting where an adapter lives is a layering violation.
    """
    imported = _imported_dotted_paths(module_path.read_text())

    offenders = {
        name
        for name in imported
        if name.startswith(("tailorcraft.application", "tailorcraft.infrastructure"))
    }

    assert not offenders, (
        f"{module_path.name} imports {sorted(offenders)}. Dependencies point inward only: "
        f"the domain is the centre and knows about nothing outside itself."
    )
