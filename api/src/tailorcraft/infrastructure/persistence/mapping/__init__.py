"""Imperative mappings, one module per aggregate.

Add a module here as `<context>/<aggregate>.py` and register it in `load_all()` below. The explicit
list is deliberate: a directory scan would be shorter and would fail silently the day a file is
renamed, whereas a missing name here is a one-line diff a reviewer can see.
"""

from __future__ import annotations


def load_all() -> None:
    """Import every mapping module for its side effect of calling `map_imperatively()`.

    `guest_session` is imported first: `base_cv`'s, `job_posting`'s and `tailoring_run`'s `Table`
    all reference `guest_session_table.c.id` for their foreign key, so the table they depend on must
    exist on `metadata` first. Import order does not affect mapper *configuration* (SQLAlchemy
    resolves that lazily), but it does affect whether the `Table` objects themselves are ready to be
    referenced.

    `tailoring_run` names a `base_cv_id` and a `job_posting_id` but has **no** foreign key to either
    table (the mapping module records both reasons), so its position after `intake` and `posting`
    here is alphabetical tidiness rather than a dependency — only its position after
    `identity.guest_session` is load-bearing.
    """
    from tailorcraft.infrastructure.persistence.mapping.identity import guest_session
    from tailorcraft.infrastructure.persistence.mapping.intake import base_cv
    from tailorcraft.infrastructure.persistence.mapping.posting import job_posting
    from tailorcraft.infrastructure.persistence.mapping.tailoring import tailoring_run

    # Imported for their side effect (each module calls `map_imperatively` at import time); the
    # assignment silences "unused import". A module missing from this list is silently unmapped —
    # which is why the list is explicit rather than a directory scan.
    _ = (guest_session, base_cv, job_posting, tailoring_run)
