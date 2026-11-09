"""`DeferredEventPublisher`: holds events until the caller's commit, then hands them on.

For a use case that publishes before the transaction it runs in commits: the router's
`record_not_queued` (slice 5.2). `RecordRunFailure` publishes after `runs.save`, which is the commit in
the worker but not in a request, where the handler commits afterwards. Wired in place of the inner
publisher, this keeps the pre-5.2 promise there: a log line never announces a `failed` run (or a
refund) that a rollback is about to un-happen.
"""

from __future__ import annotations

from tailorcraft.domain.shared.events import DomainEvent, EventPublisherPort


class DeferredEventPublisher:
    """`publish` buffers; `release` publishes the buffer to `inner` (call it after the commit);
    `discard` drops it (call it after a rollback). One per request."""

    def __init__(self, inner: EventPublisherPort) -> None:
        self._inner = inner
        self._held: list[DomainEvent] = []

    async def publish(self, *events: DomainEvent) -> None:
        self._held.extend(events)

    async def release(self) -> None:
        held, self._held = self._held, []
        await self._inner.publish(*held)

    def discard(self) -> None:
        self._held.clear()
