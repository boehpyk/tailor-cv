"""AC-1: the stricter purity rule for `domain/identity/`, on top of the codebase-wide vendor-only
walk in `api/tests/unit/test_domain_purity.py`.

That existing test proves "no third-party import anywhere under `domain/`" by default-deny over
`sys.stdlib_module_names`. It does not, and by design cannot, prove the narrower design rule this
slice adds: `domain/identity/` may import **only** the standard library and `tailorcraft.domain.shared`
— not a sibling bounded context (`domain.tailoring`, `domain.posting`, …), which would pass the
vendor-only walk without comment. Three things are pinned here, all from AC-1's text and none from
running the code:

1. every import in `domain/identity/` resolves to the standard library, `tailorcraft.domain.identity`
   itself, or `tailorcraft.domain.shared`;
2. `User` and `GuestSession` share no base class, `Protocol`, union type or type alias beyond the
   `RecordsEvents` mixin and `object` — asserted structurally over their MROs and over the AST of
   every module in the package, not by reading the two files and trusting nobody added one later;
3. `domain/identity/guest_session.py` is byte-for-byte what `main` shipped — a guest session is not a
   weak login (ADR-0010), and 2.4's claim design is built on this file staying exactly what it is.

A naive substring search for "cookie" or "sha" over `ports.py` would flag its own docstrings, which
talk about cookies and hash algorithms without importing anything about them — every check below
walks the parsed AST rather than the raw text, as `test_domain_purity.py` already does for the
identical reason.

Pure: no I/O beyond reading source files from disk, no fixtures, no event loop, no mocks.
"""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

import pytest

import tailorcraft.domain.identity
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.shared.events import RecordsEvents

IDENTITY_ROOT = Path(tailorcraft.domain.identity.__file__).parent
STDLIB = sys.stdlib_module_names

# Recorded 2026-09-23 with:
#   git show main:api/src/tailorcraft/domain/identity/guest_session.py | sha256sum
# before any slice-2.1 commit touched `domain/identity/`. If `test_guest_session_module_is_byte_
# identical_to_main` goes red, the fix is never to update this constant — it is to find out what
# touched `guest_session.py` and revert it. A changed `GuestSession` is a design decision (ADR-0010's
# territory), not a slice-2.1 detail, and belongs in its own reviewed commit.
GUEST_SESSION_SHA256 = "f020900b0884800da63e338b910fe8897c09ad7f55c3e1a98eb7f773ce444948"


def _identity_modules() -> list[Path]:
    return sorted(IDENTITY_ROOT.rglob("*.py"))


def test_the_identity_package_is_not_empty() -> None:
    """Guard the guard, as `test_domain_purity.py`'s identical test does: every assertion below
    iterates over this collection, and an empty one would make every parametrized test below pass
    while checking nothing."""
    assert len(_identity_modules()) >= 3


# ====================================================================================================
# 1. Only the standard library and `tailorcraft.domain.shared` (AC-1)
# ====================================================================================================


def _imported_top_levels(source: str) -> set[str]:
    """Every top-level package name this module imports — including inside a function body, which
    is where `value_objects.py`'s cycle-breaking imports live."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _imported_dotted_paths(source: str) -> set[str]:
    """Every module path this module imports, at full dotted length, including function-local
    imports."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module_path", _identity_modules(), ids=lambda p: p.name)
def test_identity_module_imports_only_stdlib_and_domain_shared(module_path: Path) -> None:
    """`domain/identity/` may depend on the standard library and `tailorcraft.domain.shared` —
    nothing else, and in particular no sibling context. The codebase-wide walk in
    `test_domain_purity.py` would not catch `from tailorcraft.domain.tailoring import Something`: it
    is not a vendor import, so it passes that test's default-deny, and this is the narrower rule that
    exists to catch it anyway."""
    source = module_path.read_text()

    top_levels = _imported_top_levels(source)
    non_stdlib = {name for name in top_levels if name not in STDLIB and not name.startswith("_")}
    offenders = non_stdlib - {"tailorcraft"}
    assert not offenders, (
        f"{module_path.name} imports {sorted(offenders)}, which is neither the standard library nor "
        f"tailorcraft. domain/identity/ depends on the standard library and tailorcraft.domain.shared "
        f"only (AC-1)."
    )

    dotted = _imported_dotted_paths(source)
    tailorcraft_imports = {
        name for name in dotted if name == "tailorcraft" or name.startswith("tailorcraft.")
    }
    allowed_prefixes = ("tailorcraft.domain.identity", "tailorcraft.domain.shared")
    disallowed = {name for name in tailorcraft_imports if not name.startswith(allowed_prefixes)}
    assert not disallowed, (
        f"{module_path.name} imports {sorted(disallowed)}. domain/identity/ may import only itself "
        f"and tailorcraft.domain.shared (AC-1) — a sibling context (domain.tailoring, domain.posting, "
        f"…), application or infrastructure is not identity's business."
    )


# ====================================================================================================
# 2. Nothing spans `User` and `GuestSession` (AC-1)
# ====================================================================================================


def test_user_and_guest_session_share_no_base_class_beyond_the_mixin_and_object() -> None:
    """`RecordsEvents` (composed by every aggregate that records domain events) and `object`
    (composed by everything that exists) are the only two classes any two aggregates in this
    codebase may legitimately have in common. Anything else in the intersection of `User.__mro__`
    and `GuestSession.__mro__` is a base class quietly unifying "a registered person" and "an
    anonymous, expiring session" — exactly the thing ADR-0008 and ADR-0010 say the two are not."""
    shared = (set(User.__mro__) & set(GuestSession.__mro__)) - {object, RecordsEvents}

    assert not shared, (
        f"User and GuestSession share {shared} in their MRO beyond RecordsEvents/object — AC-1 "
        f"forbids a base class spanning the two."
    )


def _union_name_sets(tree: ast.AST) -> list[set[str]]:
    """Every set of bare `Name`/`Attribute` identifiers appearing together inside a `X | Y`
    annotation (`ast.BinOp` with `ast.BitOr`) or a `typing.Union[X, Y]` subscript, anywhere in
    `tree` — module-level type aliases, function signatures, `Protocol` method signatures, all of
    it, since `ast.walk` does not care which statement contains the expression. A union spanning
    `User` and `GuestSession` at *any* level, in *any* module under `domain/identity/`, is the thing
    AC-1 forbids: "no route reads both" (contrast 1 in the feature spec) starts with "no type says
    both are the same kind of thing"."""

    def names_in(node: ast.AST) -> set[str]:
        found: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                found.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                found.add(sub.attr)
        return found

    sets: list[set[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            sets.append(names_in(node))
        elif isinstance(node, ast.Subscript):
            target = node.value
            target_name = (
                target.id
                if isinstance(target, ast.Name)
                else target.attr
                if isinstance(target, ast.Attribute)
                else None
            )
            if target_name == "Union":
                sets.append(names_in(node.slice))
    return sets


@pytest.mark.parametrize("module_path", _identity_modules(), ids=lambda p: p.name)
def test_no_union_or_alias_spans_user_and_guest_session(module_path: Path) -> None:
    tree = ast.parse(module_path.read_text())

    offenders = [names for names in _union_name_sets(tree) if {"User", "GuestSession"} <= names]

    assert not offenders, (
        f"{module_path.name} contains a union or type alias naming both User and GuestSession "
        f"({offenders}) — AC-1 forbids a union type or alias spanning the two."
    )


# ====================================================================================================
# 3. `guest_session.py` is untouched (AC-1)
# ====================================================================================================


def test_guest_session_module_is_byte_identical_to_main() -> None:
    """`domain/identity/guest_session.py` is the one file this slice may not edit. `GuestSession` is
    not a weak login (ADR-0010) and does not evolve alongside `User`; 2.4's claim design is built on
    this file staying exactly what `main` shipped before slice 2.1 started. A change here — even a
    comment, even a reformat — is a design decision about the guest session and belongs in its own
    reviewed commit with its own ADR amendment, never folded into this slice's diff.

    The digest was recorded with `git show main:api/src/tailorcraft/domain/identity/guest_session.py
    | sha256sum` on 2026-09-23. If this goes red, the fix is never to update `GUEST_SESSION_SHA256`
    — it is to find out what touched the file and revert it.
    """
    guest_session_path = IDENTITY_ROOT / "guest_session.py"

    digest = hashlib.sha256(guest_session_path.read_bytes()).hexdigest()

    assert digest == GUEST_SESSION_SHA256
