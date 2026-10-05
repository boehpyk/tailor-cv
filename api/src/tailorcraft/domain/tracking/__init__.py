"""The `tracking` bounded context: a registered user's tracked applications and the board that lists them.

A seventh context (ADR-0029, Constitution §4), because a card's stage is a different *kind* of fact
from anything `tailoring` holds: a run's status records a paid call that happened, a card's stage
records what the user currently believes about their job search — and belief is corrected.

**What this package may import is a short list** (ADR-0029 decision 1; the owner's T0 amendment;
AC-6 pins it with an AST allow-list): the standard library, `domain.shared`, and `domain.identity`
for `UserId`. **No sibling context** — never `domain.tailoring`, `domain.posting` or
`domain.intake`. The run a card references is held as tracking's own `TrackedRunRef`, and "only a
succeeded run is trackable" is the use case's rule, enforced before `TrackedApplication.track` is
called. `application/tracking/` is where `tailoring`'s types meet this context's, and it converts at
that seam.

**This package `__init__` re-exports nothing** — the rule `domain/intake/__init__.py`,
`domain/export/__init__.py` and `domain/retention/__init__.py` follow, argued once in
`domain/shared/files.py`'s module docstring.
"""
