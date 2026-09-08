"""The only `EventPublisherPort` adapter this slice has: log every released event.

**This module is the enforcement point for "an event carries no CV body."** `domain/shared/events.py`
and every event in `domain/intake/events.py` *say* an event payload is ids and value objects only —
but a docstring is a promise, and this is where the promise is kept or broken. Every field of every
event that reaches `publish()` is logged, generically, with no per-event allow-list. That is
deliberate: a hand-written field list here (`if isinstance(event, BaseCvUploaded): log(...)`) would
have to be updated every time a new event is added, and the day someone forgets is the day a CV body
leaks into a log line and nothing catches it. Logging *every* field of *every* event instead means
the guard is structural — AC-13's test can upload a real CV, capture structlog's output, and assert
the fixture's text and filename never appear, and that assertion stays true for events nobody has
written yet.

If a future event ever gains a field that is not an id, a value object or a scalar, **this is where
it appears in a log file.** That is the point: catch it here, not in a support ticket.
"""

from __future__ import annotations

import dataclasses
import enum
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

from tailorcraft.domain.shared.events import DomainEvent

log = structlog.get_logger(__name__)


def _to_loggable(value: object) -> object:
    """Normalize one field's value into something JSON-serializable and safe to log.

    Generic on purpose, matching the class-level docstring: this is not a per-field or per-event
    switch, it is a structural walk over whatever `dataclasses.fields()` reports. A typed id
    (`BaseCvId`, `GuestSessionId`, …) is itself a frozen dataclass wrapping a `UUID`, so it is
    unwrapped recursively; a `StrEnum` (`CvContentType`, `ExtractionFailureReason`, …) already *is*
    a `str`, but is normalized through `.value` for a stable, JSON-plain scalar rather than relying
    on `str` subclassing.
    """
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_loggable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    return value


class LoggingEventPublisher:
    """`EventPublisherPort` that logs each event's class name and field set via structlog.

    Called by a use case **after** the aggregate that recorded the events has been saved
    (`domain/shared/events.py`'s own docstring says why) — this adapter has no opinion about that
    ordering, it only logs what it is handed.
    """

    async def publish(self, *events: DomainEvent) -> None:
        for event in events:
            fields: dict[str, Any] = {
                f.name: _to_loggable(getattr(event, f.name)) for f in dataclasses.fields(event)
            }
            # `event_type`, not `event`: structlog's `BoundLogger.info(event, **kwargs)` already
            # treats its first positional argument as the log line's own `event` field (here,
            # the literal string "domain_event") — passing a *second* `event=` keyword on top of
            # that collides with it (`TypeError: ... got multiple values for argument 'event'`),
            # found by running this adapter for the first time rather than by reading it.
            log.info("domain_event", event_type=type(event).__name__, **fields)
