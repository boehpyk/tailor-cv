"""The history cursor's wire encoding (slice 2.3, technical plan §0.5, ADR-0024).

`HistoryCursor` is a domain value object — `(requested_at, tailoring_run_id)`, the last entry of a
page. Its **encoding** is the router's: base64url (no padding) of `"<epoch_seconds>.<uuid>"`, opaque
to the client, which only ever echoes back what `next_cursor` handed it.

**Unsigned, on purpose** (ADR-0024): the cursor carries nothing the requester could not already see,
and the history query is scoped to the bearer's user id, which is not in the cursor — so a forged
cursor can only move a user around their own history.

**Decoded at the boundary, refused whole**: bad base64, a missing or extra `.`, a non-integer or
negative epoch, a malformed UUID, and anything `HistoryCursor` itself refuses all become
`InvalidHistoryCursor` → 422 `invalid_cursor` (H-26), with one fixed sentence. **The cursor is never
logged**, not even when refused.

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

from tailorcraft.domain.tailoring.errors import InvalidHistoryCursor
from tailorcraft.domain.tailoring.history import HistoryCursor
from tailorcraft.domain.tailoring.value_objects import TailoringRunId

# Digits only: no sign, no space, no underscore (`int()` accepts all three).
_EPOCH_SECONDS: Final = re.compile(r"[0-9]{1,12}")
# The fixed refusal — never `str(exc)` of whatever the parse tripped on, which can quote the input.
_REFUSAL: Final = "not a history cursor"


def encode_cursor(cursor: HistoryCursor) -> str:
    """`cursor` as the opaque string a `HistoryPageResponse.next_cursor` carries."""
    seconds = int(cursor.requested_at.timestamp())
    raw = f"{seconds}.{cursor.tailoring_run_id.value}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(raw: str) -> HistoryCursor:
    """The `HistoryCursor` a `?cursor=` string encodes; `InvalidHistoryCursor` on any failure."""
    try:
        padded = raw + "=" * (-len(raw) % 4)
        text = base64.b64decode(padded, altchars=b"-_", validate=True).decode("ascii")
        seconds, run_id = text.split(".")
        if not _EPOCH_SECONDS.fullmatch(seconds):
            raise ValueError(_REFUSAL)
        requested_at = datetime.fromtimestamp(int(seconds), tz=UTC)
        tailoring_run_id = TailoringRunId(UUID(run_id))
    except (binascii.Error, UnicodeDecodeError, ValueError, OverflowError, OSError):
        # `from None`: the chain would carry the input a stranger sent, and the input is never
        # logged or echoed (H-26).
        raise InvalidHistoryCursor(_REFUSAL) from None
    return HistoryCursor(requested_at=requested_at, tailoring_run_id=tailoring_run_id)
