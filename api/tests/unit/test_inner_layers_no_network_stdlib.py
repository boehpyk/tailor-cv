"""AC-26: no network-capable standard library in `domain/` or `application/`.

`smtplib`, `email`, `ssl`, `socket`, `http` and `urllib.request` are how a process talks to the
outside world. The inner layers speak to it only through ports, so none of them may appear there.

**Why a pytest scan and not (only) import-linter.** Checked before relying on it: with
`include_external_packages = true` import-linter *does* see standard-library modules, so five of the
six could be a `forbidden` contract. It cannot take the sixth: external packages are squashed to
their root, so forbidding `urllib.request` means forbidding `urllib`, which would refuse
`urllib.parse` (a pure string function `domain/posting` legitimately uses). This scan matches full
dotted paths, handles `from urllib import request`, and covers both layers uniformly.

Matching is on **module names**, never substrings: `tailorcraft.domain.identity.email_address` or an
identifier called `socket_count` must not trip it. Each forbidden module has a positive control: a
synthetic source importing it, in every import form, must be reported. A scan that cannot be seen
failing is a docblock.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import tailorcraft.application
import tailorcraft.domain

FORBIDDEN = ("smtplib", "email", "ssl", "socket", "http", "urllib.request")

_LAYERS = {
    "domain": Path(tailorcraft.domain.__file__).parent,
    "application": Path(tailorcraft.application.__file__).parent,
}


def _imported_modules(source: str) -> set[str]:
    """Every absolute module path imported, with `from m import n` also yielding `m.n` (so
    `from urllib import request` is `urllib.request`). Relative imports stay inside the layer."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def forbidden_imports(source: str) -> set[str]:
    return {
        name
        for name in _imported_modules(source)
        for banned in FORBIDDEN
        if name == banned or name.startswith(banned + ".")
    }


def _layer_files() -> list[tuple[str, Path]]:
    return [(layer, path) for layer, root in _LAYERS.items() for path in sorted(root.rglob("*.py"))]


def test_both_layers_are_actually_scanned() -> None:
    """Guard the guard: an empty collection would make every parametrised case below vanish."""
    scanned = {layer for layer, _ in _layer_files()}
    assert scanned == {"domain", "application"}
    assert len(_layer_files()) >= 50


@pytest.mark.parametrize(
    ("layer", "path"), _layer_files(), ids=lambda v: v.name if isinstance(v, Path) else v
)
def test_an_inner_layer_module_imports_no_network_stdlib(layer: str, path: Path) -> None:
    offenders = forbidden_imports(path.read_text())

    assert not offenders, (
        f"{layer}/{path.name} imports {sorted(offenders)}. The inner layers reach the network and "
        f"the mail server only through ports; put the call in infrastructure/ (AC-26)."
    )


# ----------------------------------------------------------------------------- positive controls


@pytest.mark.parametrize("module", FORBIDDEN)
@pytest.mark.parametrize("form", ["import {m}", "import {m} as alias", "from {p} import {leaf}"])
def test_the_scan_reports_each_forbidden_module_in_each_import_form(module: str, form: str) -> None:
    parent, _, leaf = module.rpartition(".")
    if "{p}" in form and not parent:
        parent, leaf = module, "anything"
    source = form.format(m=module, p=parent, leaf=leaf)

    assert forbidden_imports(source), f"{source!r} was not reported"


@pytest.mark.parametrize(
    "source",
    [
        "import email.utils",
        "from email.message import EmailMessage",
        "import http.client",
        "from http import HTTPStatus",
        "from urllib import request",
        "import urllib.request",
        "def f():\n    import socket\n",
    ],
)
def test_the_scan_reports_submodules_and_function_local_imports(source: str) -> None:
    assert forbidden_imports(source)


@pytest.mark.parametrize(
    "source",
    [
        "import urllib.parse",
        "from urllib.parse import urlsplit",
        "from tailorcraft.domain.identity.email_address import EmailAddress",
        "from tailorcraft.domain.identity import email",
        "from . import email",
        "from .email import X",
        "import emailvalidator",
        "socket_count = 1\nhttp_status = 200",
        "import ssl_helpers",
    ],
)
def test_the_scan_does_not_trip_on_lookalike_names(source: str) -> None:
    assert forbidden_imports(source) == set()
