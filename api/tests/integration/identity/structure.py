"""Structural proofs for the 2.5 use cases whose point is what they are *not given* (AC-7, AC-8,
AC-11): a class cannot call a port its constructor never received, and a module that never names
`UserRepository` or `find_by_email` cannot branch on the address.

Two checks, because each alone has a hole. The **constructor's resolved type hints** prove the
dependency is not injected; the **module's AST** proves it is not reached some other way (an import
inside the method, a module-level singleton). A recording double that "records zero calls" cannot
make this claim — the class is never handed it.
"""

from __future__ import annotations

import ast
import inspect
import typing
from collections.abc import Iterable


def constructor_dependency_names(cls: type) -> set[str]:
    """Names of every type the constructor's parameters resolve to (annotations are strings under
    `from __future__ import annotations`, so they are resolved against the module's globals)."""
    hints = typing.get_type_hints(cls.__init__)  # type: ignore[misc]
    hints.pop("return", None)
    return {getattr(h, "__name__", repr(h)) for h in hints.values()}


def referenced_names(source: str) -> set[str]:
    """Every identifier the source names: `Name` ids, `Attribute` attrs, imported names and aliases."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom | ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[-1])
                if alias.asname:
                    found.add(alias.asname)
    return found


def module_references(cls: type, names: Iterable[str]) -> set[str]:
    """Which of `names` the class's whole module source mentions (docstrings are strings, not
    identifiers, so prose about the rule does not trip it)."""
    module = inspect.getmodule(cls)
    assert module is not None
    return set(names) & referenced_names(inspect.getsource(module))
