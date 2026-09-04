"""Imperative mappings, one module per aggregate.

Add a module here as `<context>/<aggregate>.py` and register it in `load_all()` below. The explicit
list is deliberate: a directory scan would be shorter and would fail silently the day a file is
renamed, whereas a missing name here is a one-line diff a reviewer can see.

Empty in Phase 0 — the first aggregate lands with slice 1.1.
"""

from __future__ import annotations


def load_all() -> None:
    """Import every mapping module for its side effect of calling `map_imperatively()`."""
    # from tailorcraft.infrastructure.persistence.mapping.intake import base_cv
    return
