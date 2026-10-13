"""The `SummarizeStorage` use case: how the uploads volume splits between guests, users and
nobody (slice 4.4, AC-7, ADR-0035, technical plan §0.5).

**A walk plus one lookup, never a read**: one `scan_older_than` over the whole volume (a cutoff in
the future, so nothing is too young to count) and **one** `owners_of` over the recognised,
non-partial keys; then pure aggregation. Sizes come from `ScannedFile.size_bytes`, which the walk
already holds. An `owners_of` failure escapes: no half-attributed overview. Read-only; unlinks
nothing.
"""

from __future__ import annotations

from tailorcraft.domain.retention.ports import ExpiredGuestDataPort, OrphanFileScannerPort
from tailorcraft.domain.retention.storage import StorageOverview
from tailorcraft.domain.shared.clock import Clock


class SummarizeStorage:
    """Flow of `__call__`: `now = clock.now()` → `scanned = await scanner.scan_older_than(cutoff)`
    → `owners = await data.owners_of(recognised non-partial refs)` → bucket each file: `ref is
    None` → unrecognized; `is_partial` → partial; owner `GUEST` / `USER` → guest / user; absent →
    unreferenced → `StorageOverview(…, scanned_at=now)`."""

    def __init__(
        self, scanner: OrphanFileScannerPort, data: ExpiredGuestDataPort, clock: Clock
    ) -> None:
        self._scanner = scanner
        self._data = data
        self._clock = clock

    async def __call__(self) -> StorageOverview:
        raise NotImplementedError
