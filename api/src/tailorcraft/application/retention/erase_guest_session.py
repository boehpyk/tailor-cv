"""The `EraseGuestSession` use case: an admin deletes one guest session's data now — rows
committed, then files (slice 4.4, AC-6, ADR-0035, technical plan §0.3).

**The purge's step, narrowed, not a twin of it**: the body is `purge_one`, the per-session step
lifted out of `PurgeExpiredGuestSessions`, so "what purging a session is" has one implementation.
Two things differ from the batch, both because there is exactly one session: a `delete_session`
raise **escapes** (there is no next candidate to continue to), and a skip (`deleted=False` — a
claim, a purge or another erase won) is `GuestSessionNotFound`, with no file unlinked, because
those keys may name a user's files now. No expiry predicate: an expired session is erasable too.

`data` must be the **committing** adapter, as for the purge: "rows committed, then files" is the
adapter's promise, never this layer's (ADR-0002). **This layer does not log**: unlink failures are
returned as type names in the report, and the route writes the lines.
"""

from __future__ import annotations

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.retention.ports import ExpiredGuestDataPort
from tailorcraft.domain.retention.value_objects import GuestSessionErasureReport
from tailorcraft.domain.shared.files import FileStorePort


class EraseGuestSession:
    """Flow of `__call__`: `candidate = await data.session_with_files(session_id)`; `None` →
    `GuestSessionNotFound` → `outcome = await purge_one(data, files, candidate)` (a raise escapes);
    `deleted=False` → `GuestSessionNotFound` → `GuestSessionErasureReport(files_unlinked,
    file_unlink_failures)`."""

    def __init__(self, data: ExpiredGuestDataPort, files: FileStorePort) -> None:
        self._data = data
        self._files = files

    async def __call__(self, session_id: GuestSessionId) -> GuestSessionErasureReport:
        raise NotImplementedError
