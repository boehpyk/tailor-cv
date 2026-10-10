"""The keyset cursor's wire encoding: an instant and a UUID (slice 2.3, ADR-0024; shared in 4.2).

A keyset page ends on `(at, id)`, the sort key of its last row. Its **encoding** is the router's:
base64url (no padding) of `"<epoch_seconds>.<uuid>"`, opaque to the client, which only ever echoes
back what `next_cursor` handed it. History's cursor and the user directory's are both this shape,
so the codec lives once here; each router wraps a `ValueError` into its own domain error
(`_history_cursor.py`), so the refusal still names the right thing.

**Unsigned, on purpose** (ADR-0024): a cursor carries nothing the requester could not already see,
and each query is scoped by something that is not in the cursor.

**Refused whole**: bad base64, a missing or extra `.`, a non-integer or negative epoch and a
malformed UUID all become one `ValueError` with one fixed sentence — never `str(exc)` of whatever
the parse tripped on, which can quote the input. **A cursor is never logged.**

**Measured, not assumed:** `base64.urlsafe_b64decode` *discards* characters outside the alphabet
by default, so `"not-base64!!"` would decode to something. The decoder passes `validate=True` and
the URL-safe alphabet explicitly, so any foreign character is a refusal.
"""

from __future__ import annotations

import base64
import binascii
import re
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

# Digits only: no sign, no space, no underscore (`int()` accepts all three).
_EPOCH_SECONDS: Final = re.compile(r"[0-9]{1,12}")
_REFUSAL: Final = "not a keyset cursor"


def encode_keyset(at: datetime, id_: UUID) -> str:
    """`(at, id_)` as the opaque string a page's `next_cursor` carries (whole seconds)."""
    raw = f"{int(at.timestamp())}.{id_}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_keyset(raw: str) -> tuple[datetime, UUID]:
    """The `(at, id)` a cursor string encodes; `ValueError` with a fixed sentence on any failure."""
    try:
        padded = raw + "=" * (-len(raw) % 4)
        text = base64.b64decode(padded, altchars=b"-_", validate=True).decode("ascii")
        seconds, id_ = text.split(".")
        if not _EPOCH_SECONDS.fullmatch(seconds):
            raise ValueError(_REFUSAL)
        return datetime.fromtimestamp(int(seconds), tz=UTC), UUID(id_)
    except (binascii.Error, UnicodeDecodeError, ValueError, OverflowError, OSError):
        # `from None`: the chain would carry the input a stranger sent.
        raise ValueError(_REFUSAL) from None
