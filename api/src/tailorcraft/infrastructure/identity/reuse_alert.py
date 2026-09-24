"""`identity.refresh_reuse_detected` at `warning` (I-24): the one identity event that is an alarm.

`RefreshLogin` publishes `RefreshTokenReuseDetected` and then raises; the router answers the 401. The
router never sees the event, and the event is the only place the four facts I-24's line needs travel
together (`login_id`, `user_id`, `generation_presented`, `generation_current`) — so the line is
written here, where the event passes, rather than by widening the error to carry them.

**A decorator around `EventPublisherPort`, not a branch inside `LoggingEventPublisher`.** That adapter
logs every field of every event with no per-event switch, and its docstring says why that must stay
structural. This wrapper leaves it untouched (the generic `domain_event` line is still written, at
`info`) and adds the alarm on top, bound only where `RefreshLogin` is built (`deps.get_refresh_login`).

**Never either hash** — the event carries none, and this module reads only the four fields above.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Final

import structlog

from tailorcraft.domain.identity.events import RefreshTokenReuseDetected
from tailorcraft.domain.shared.events import DomainEvent, EventPublisherPort

log = structlog.get_logger(__name__)

EVENT_REFRESH_REUSE_DETECTED: Final = "identity.refresh_reuse_detected"


class ReuseAlertingEventPublisher:
    """Delegate every event to `inner`, then write one `warning` per `RefreshTokenReuseDetected`.

    **The warning never raises.** It runs inside the request whose response must *commit* the
    revocation (technical plan §2): an exception escaping here would roll the deletion back and leave
    the thief's login alive. A failure to record the alarm is strictly better than that.
    """

    def __init__(self, inner: EventPublisherPort) -> None:
        self._inner = inner

    async def publish(self, *events: DomainEvent) -> None:
        await self._inner.publish(*events)
        for event in events:
            if isinstance(event, RefreshTokenReuseDetected):
                with contextlib.suppress(Exception):
                    log.warning(
                        EVENT_REFRESH_REUSE_DETECTED,
                        login_id=str(event.login_id.value),
                        user_id=str(event.user_id.value),
                        generation_presented=event.generation_presented,
                        generation_current=event.generation_current,
                    )


if TYPE_CHECKING:
    # Makes mypy prove the class structurally satisfies the port. Never executed.
    def _assert_implements_event_publisher(publisher: ReuseAlertingEventPublisher) -> None:
        _: EventPublisherPort = publisher
