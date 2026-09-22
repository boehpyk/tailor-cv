"""Core-SQL adapters for the `retention` context's data port (ADR-0007, ADR-0018).

**Not under `repositories/`, and the directory is the argument.** Everything in that package loads
and saves *aggregates*: a `SqlAlchemy<X>Repository` hands back a domain object the ORM instrumented,
and its module docstring warns about querying private mapped attributes because that is what mapping
an aggregate costs. `ExpiredGuestDataPort` never loads one — it counts rows, reads two columns off
two tables, and issues one `DELETE` that the cascades finish. Filing it beside four repositories
would invite the next reader to make it a fifth.
"""

from __future__ import annotations
