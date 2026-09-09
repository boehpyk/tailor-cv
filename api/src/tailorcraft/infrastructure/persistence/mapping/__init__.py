"""Imperative mappings, one module per aggregate.

Add a module here as `<context>/<aggregate>.py` and register it in `load_all()` below. The explicit
list is deliberate: a directory scan would be shorter and would fail silently the day a file is
renamed, whereas a missing name here is a one-line diff a reviewer can see.
"""

from __future__ import annotations


def load_all() -> None:
    """Import every mapping module for its side effect of calling `map_imperatively()`.

    `guest_session` is imported first: `base_cv`'s `Table` references
    `guest_session_table.c.id` for its foreign key, so the table it depends on must exist on
    `metadata` first. Import order does not affect mapper *configuration* (SQLAlchemy resolves that
    lazily), but it does affect whether the `Table` objects themselves are ready to be referenced.
    """
    from tailorcraft.infrastructure.persistence.mapping.identity import guest_session
    from tailorcraft.infrastructure.persistence.mapping.intake import base_cv

    _ = (guest_session, base_cv)  # imported for their side effect; silence "unused import"
