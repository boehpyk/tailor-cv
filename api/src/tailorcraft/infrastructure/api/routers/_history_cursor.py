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

The codec itself is `_keyset_cursor.py` (shared with the user directory since slice 4.2); this
module wraps it in history's types and history's refusal.
"""

from __future__ import annotations

from typing import Final

from tailorcraft.domain.tailoring.errors import InvalidHistoryCursor
from tailorcraft.domain.tailoring.history import HistoryCursor
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.api.routers._keyset_cursor import decode_keyset, encode_keyset

# The fixed refusal — never `str(exc)` of whatever the parse tripped on, which can quote the input.
_REFUSAL: Final = "not a history cursor"


def encode_cursor(cursor: HistoryCursor) -> str:
    """`cursor` as the opaque string a `HistoryPageResponse.next_cursor` carries."""
    return encode_keyset(cursor.requested_at, cursor.tailoring_run_id.value)


def decode_cursor(raw: str) -> HistoryCursor:
    """The `HistoryCursor` a `?cursor=` string encodes; `InvalidHistoryCursor` on any failure."""
    try:
        requested_at, run_id = decode_keyset(raw)
    except ValueError:
        # `from None`: the input is never logged or echoed (H-26).
        raise InvalidHistoryCursor(_REFUSAL) from None
    return HistoryCursor(requested_at=requested_at, tailoring_run_id=TailoringRunId(run_id))
