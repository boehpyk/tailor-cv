"""AC-1's *design* assertion: `domain/retention/` may import `domain.shared` and `domain.identity`,
and must never import `domain.intake`, `domain.posting`, `domain.tailoring` or `domain.export`
(technical-plan.md §0, *"What `retention` may import"*).

**Why this is a separate test from `tests/unit/test_domain_purity.py`, not a duplicate of it.**
That module's AST walk already covers every file under `domain/`, `retention`'s four included, and
already answers one question well: *"is this import third-party?"* — anything not stdlib and not
`tailorcraft` itself fails it. import-linter's `forbidden` contract answers an adjacent question,
*"does this import a vendor somebody remembered to name?"* Neither gate can see what this test
checks: a **same-layer** import of `tailorcraft.domain.intake` is perfectly legal Python, imports
nothing third-party, and names no vendor on any forbidden list — so both existing gates would wave
it through in silence. This is a design rule, not a boundary rule: `intake`/`posting`/`tailoring`/
`export` rows are reached only through the database cascade and their files only through a
**derived** `FileRef` key (ADR-0016). The day `retention` needs to import one of those packages
directly, the cascade contract or `FileRef`'s determinism guarantee has stopped holding — which is a
conversation to have on purpose, not an import a reviewer skims past in a diff.

**The walk reads each retention module's own AST — it does not import the package and inspect
`sys.modules`.** `domain/shared/files.py` itself imports `tailorcraft.domain.intake` and
`tailorcraft.domain.export`, by its own docstring's admission, so that `FileRef.for_base_cv` and
`FileRef.for_export` can derive storage keys from their aggregates' ids — that is `shared`'s business,
argued in its own module, and importing `shared` is not the same claim as importing `intake` or
`export` directly. A `sys.modules`-after-import check cannot tell a *direct* dependency of
`retention` from a *transitive* one three hops downstream through `shared`; it would fail this test
for a reason that has nothing to do with `retention`'s own design. Reading only the `ast.Import` /
`ast.ImportFrom` nodes that appear literally inside each `domain/retention/*.py` file is the only way
to ask what `retention` itself names, as opposed to what it pulls in by way of a package that is
allowed to pull in more.

Constitution §4 · ADR-0002 · ADR-0018 · docs/specs/retention-guest-purge/technical-plan.md §0.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import tailorcraft.domain.retention as retention

RETENTION_ROOT = Path(retention.__file__).parent

# The two `domain.*` sub-packages retention is allowed to name (technical-plan.md §0's table).
_ALLOWED_DOMAIN_PACKAGES = frozenset({"shared", "identity"})

# The four sub-packages named explicitly in AC-1 and the technical plan as never-to-be-imported:
# their rows go by the database cascade and their files by a derived `FileRef` key.
_FORBIDDEN_DOMAIN_PACKAGES = frozenset({"intake", "posting", "tailoring", "export"})


def _retention_modules() -> list[Path]:
    return sorted(RETENTION_ROOT.rglob("*.py"))


def _imported_domain_packages(source: str) -> set[str]:
    """Every `tailorcraft.domain.<package>` named by a **direct** import in this source text.

    Walks the parsed AST of the file's own content — never `sys.modules`, which would also surface
    everything `domain.shared` imports on `retention`'s behalf (see module docstring above) and could
    not distinguish a direct dependency from an incidental transitive one.

    `node.level == 0` on `ast.ImportFrom` skips *relative* imports (`from .value_objects import
    ...`), which name no top-level path to check and are by construction imports of `retention`
    itself. `ports.py` reaches `value_objects.py` by its **absolute** dotted path instead
    (`from tailorcraft.domain.retention.value_objects import ...`), which is the same "importing my
    own package" fact spelled a different way — so `retention` is excluded from the result here too,
    the same way a relative import would have been. AC-1's question is what `retention` imports of
    *other* contexts, never what it imports of itself.
    """
    packages: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("tailorcraft.domain."):
                    packages.add(alias.name.split(".")[2])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.startswith("tailorcraft.domain."):
                packages.add(node.module.split(".")[2])
            elif node.module == "tailorcraft.domain":
                # `from tailorcraft.domain import identity` — the sub-package is the imported name.
                packages.update(alias.name for alias in node.names)
    packages.discard("retention")
    return packages


def test_the_retention_package_is_not_empty() -> None:
    """Guard the guard: every assertion below iterates `_retention_modules()`. If that collection
    were ever empty — a moved package, a renamed directory — every parametrized test in this module
    would report green while checking nothing at all."""
    assert len(_retention_modules()) >= 3


@pytest.mark.parametrize("module_path", _retention_modules(), ids=lambda p: p.name)
def test_retention_module_names_only_shared_and_identity_from_domain(module_path: Path) -> None:
    """Every `tailorcraft.domain.*` import inside `domain/retention/` names `shared` or `identity`
    and nothing else — the positive half of technical-plan.md §0's import table (AC-1)."""
    imported = _imported_domain_packages(module_path.read_text())

    offenders = imported - _ALLOWED_DOMAIN_PACKAGES

    assert not offenders, (
        f"{module_path.relative_to(RETENTION_ROOT.parent.parent)} imports domain.{sorted(offenders)} "
        f"directly. `retention` may import only `domain.shared` and `domain.identity` "
        f"(technical-plan.md §0). If this package genuinely needs something else, that is a design "
        f"change to argue in the spec, not an import to add."
    )


@pytest.mark.parametrize("module_path", _retention_modules(), ids=lambda p: p.name)
def test_retention_module_never_imports_intake_posting_tailoring_or_export(
    module_path: Path,
) -> None:
    """The negative half, asserted explicitly rather than left to be implied by the positive test
    above: none of `intake`, `posting`, `tailoring`, `export` is ever named directly. Their rows go
    by the database cascade and their files by a derived `FileRef` key; an import of one of them here
    would mean that contract had stopped holding (AC-1, technical-plan.md §0)."""
    imported = _imported_domain_packages(module_path.read_text())

    offenders = imported & _FORBIDDEN_DOMAIN_PACKAGES

    assert not offenders, (
        f"{module_path.relative_to(RETENTION_ROOT.parent.parent)} imports domain.{sorted(offenders)} "
        f"directly. The purge reaches those contexts' rows only through the database cascade and "
        f"their files only through a derived `FileRef` key (ADR-0016) — never by loading their "
        f"aggregates. If this import is deliberate, the cascade contract has broken and that is a "
        f"conversation, not a diff."
    )
