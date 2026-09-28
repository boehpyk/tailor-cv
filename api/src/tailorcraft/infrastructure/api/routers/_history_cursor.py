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

**SKELETON (T20).** Both functions raise `NotImplementedError`; T23 implements them after `qa`'s
T21 records the red.
"""

from __future__ import annotations

from tailorcraft.domain.tailoring.history import HistoryCursor


def encode_cursor(cursor: HistoryCursor) -> str:
    """`cursor` as the opaque string a `HistoryPageResponse.next_cursor` carries."""
    raise NotImplementedError


def decode_cursor(raw: str) -> HistoryCursor:
    """The `HistoryCursor` a `?cursor=` string encodes; `InvalidHistoryCursor` on any failure."""
    raise NotImplementedError
