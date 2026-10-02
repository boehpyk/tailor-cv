"""The `PurgeExpiredIdentityTokens` use case: delete identity rows that have aged out — pending
registrations, password resets and logins past their `expires_at` (slice 2.5, technical plan §0.9,
AC-15, V-57, V-58).

Read it beside `PurgeExpiredGuestSessions` and do not merge them: that purge deletes a person's work
and unlinks files, behind a flag and a rehearsal; this one deletes rows every code path already
refuses, owns no files, and is on by default.

**Bounded batches.** Per kind, `delete_expired_*(as_of, batch_size)` is called until a batch deletes
nothing, so a backlog never becomes one long statement. Each call is durable on return (the port's
contract), so a failure mid-run keeps what already went and the next tick carries on.

**`as_of` is an argument**, not read from a `Clock` here: the beat task reads the clock once and
passes it in, so the one instant is visible at the call and a test names it directly (AC-15's
`PurgeExpiredIdentityTokens(as_of)`).

**This layer does not log**; the counts are returned and the task writes the line. No command
dataclass: an instant, the purge's precedent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from tailorcraft.domain.retention.ports import ExpiredIdentityTokenPort
from tailorcraft.domain.retention.value_objects import IdentityTokenSweepReport


class PurgeExpiredIdentityTokens:
    """Delete every pending registration, password reset and login expired at `as_of`.

    `batch_size` is the most rows one statement may delete (the plan's 1 000), fixed at construction.

    Flow of `__call__`: for pending registrations, then resets, then logins, loop
    `await tokens.delete_expired_<kind>(as_of, batch_size)` summing the counts until a call returns
    0; return `IdentityTokenSweepReport(pending_registrations, password_resets, logins)`. A port
    failure propagates (V-57: the task fails, the next tick retries).
    """

    def __init__(self, tokens: ExpiredIdentityTokenPort, batch_size: int) -> None:
        self._tokens = tokens
        self._batch_size = batch_size

    async def __call__(self, as_of: datetime) -> IdentityTokenSweepReport:
        return IdentityTokenSweepReport(
            pending_registrations=await self._drain(self._tokens.delete_expired_pending, as_of),
            password_resets=await self._drain(self._tokens.delete_expired_resets, as_of),
            logins=await self._drain(self._tokens.delete_expired_logins, as_of),
        )

    async def _drain(
        self, delete_batch: Callable[[datetime, int], Awaitable[int]], as_of: datetime
    ) -> int:
        """Call `delete_batch(as_of, batch_size)` until a batch deletes nothing; the total."""
        total = 0
        while deleted := await delete_batch(as_of, self._batch_size):
            total += deleted
        return total
