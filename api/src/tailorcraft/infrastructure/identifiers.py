"""UUIDv7 generation.

Identity is **application-assigned** (ADR-0007): the repository mints an id via `next_identity()`
so an aggregate is fully valid before it ever meets the database. That is what makes domain tests
possible without one, and it is why there is no `id: int | None` anywhere in this codebase waiting
to be filled in by a `flush()`.

Version 7 rather than 4 because it is time-ordered: sequential ids insert at the right-hand edge of
a B-tree instead of scattering across it, which keeps index pages dense and writes cheap. Random v4
primary keys are one of the classic ways a table gets slow without anything in the query changing.

Implemented here in ~15 lines of standard library rather than pulled from a package: `uuid.uuid7()`
lands in the standard library in Python 3.14, and the layout below is exactly what it produces, so
this file becomes a two-line delegation the day we move.

    0                   1                   2                   3
    |unix_ts_ms (48 bits)         |ver|rand_a (12)|var|  rand_b (62)   |
"""

from __future__ import annotations

import secrets
import time
from uuid import UUID


def uuid7() -> UUID:
    """Return a time-ordered UUID version 7."""
    unix_ts_ms = time.time_ns() // 1_000_000

    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)

    value = unix_ts_ms << 80
    value |= 0x7 << 76  # version 7
    value |= rand_a << 64
    value |= 0b10 << 62  # RFC 4122 variant
    value |= rand_b

    return UUID(int=value)
