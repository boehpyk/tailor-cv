"""Core-SQL adapters for the `identity` context that load no aggregate of their own (ADR-0007,
ADR-0025).

**Not under `repositories/`**, for `persistence/retention/`'s reason: everything in that package loads
and saves aggregates. `GuestWorkClaimPort` locks one `GuestSession` and then re-keys four other
contexts' tables with Core statements; filing it beside the repositories would invite the next reader
to make it one.
"""

from __future__ import annotations
