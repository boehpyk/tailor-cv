"""AC-1's design assertion for the claim: `domain/identity/` names no sibling context's domain package.

`identity` may import `domain.shared` (and itself) and must never import `domain.intake`,
`domain.posting`, `domain.tailoring`, `domain.export` — or `domain.retention`, which would be the
reverse of the dependency ADR-0025 chose (`unlink_failures` is `tuple[str, ...]` precisely so that
`identity` does not import retention's `FileUnlinkFailure`).

Same shape as `tests/unit/retention/test_import_allowlist.py`, and for the same reason: a same-layer
import of a sibling context is legal Python, third-party-free and on no forbidden list, so
`test_domain_purity.py` and import-linter both wave it through. It walks each module's own AST, not
`sys.modules` — `domain/shared/files.py` itself imports `intake` and `export`, and a transitive
import through `shared` is `shared`'s business, not `identity`'s.

This test **passes on arrival** (the skeleton imports only `domain.shared`); it is a regression
guard for T6's GREEN, not a red. `test_domain_purity.py` in this directory already enforces the
positive half per module; the explicit negative half is restated here by name so a failure reads as
the rule it breaks.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import tailorcraft.domain.identity as identity

IDENTITY_ROOT = Path(identity.__file__).parent

_ALLOWED_DOMAIN_PACKAGES = frozenset({"shared"})
_FORBIDDEN_DOMAIN_PACKAGES = frozenset({"intake", "posting", "tailoring", "export", "retention"})


def _identity_modules() -> list[Path]:
    return sorted(IDENTITY_ROOT.rglob("*.py"))


def _imported_domain_packages(source: str) -> set[str]:
    """Every `tailorcraft.domain.<package>` named by a direct import, function-local ones included,
    excluding `identity` itself."""
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
                packages.update(alias.name for alias in node.names)
    packages.discard("identity")
    return packages


def test_the_identity_package_includes_the_claim_module() -> None:
    """Guard the guard: the parametrized tests below must actually cover `claim.py`."""
    assert "claim.py" in {p.name for p in _identity_modules()}


@pytest.mark.parametrize("module_path", _identity_modules(), ids=lambda p: p.name)
def test_identity_module_names_only_shared_from_domain(module_path: Path) -> None:
    imported = _imported_domain_packages(module_path.read_text())

    offenders = imported - _ALLOWED_DOMAIN_PACKAGES

    assert not offenders, (
        f"{module_path.name} imports domain.{sorted(offenders)} directly. `identity` may import "
        f"only `domain.shared` from another package (ADR-0025, technical-plan.md §1)."
    )


@pytest.mark.parametrize("module_path", _identity_modules(), ids=lambda p: p.name)
def test_identity_module_never_imports_intake_posting_tailoring_export_or_retention(
    module_path: Path,
) -> None:
    imported = _imported_domain_packages(module_path.read_text())

    offenders = imported & _FORBIDDEN_DOMAIN_PACKAGES

    assert not offenders, (
        f"{module_path.name} imports domain.{sorted(offenders)} directly. The claim moves other "
        f"contexts' rows in one adapter transaction and names them only as counts and `FileRef`s "
        f"(ADR-0025); an import of their types here means that boundary has gone."
    )


def test_the_walk_covers_slice_2_5s_new_modules() -> None:
    """Slice 2.5 AC-6: guard the guard for the modules it added, so the allow-list cannot pass by
    not looking at them."""
    names = {p.name for p in _identity_modules()}

    assert {"account_mail.py", "pending_registration.py", "password_reset.py"} <= names
