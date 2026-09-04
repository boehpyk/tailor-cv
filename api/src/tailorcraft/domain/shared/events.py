"""Domain events, and the aggregate-side mixin that records them.

The rule that makes events safe here is about *who dispatches*: an aggregate **records** that
something happened and never publishes it. The application layer releases the recorded events and
publishes them **after** a successful save. An aggregate that dispatched directly would announce a
fact that a failed transaction is about to un-happen.

What an event may carry is equally load-bearing, and for this product it is a privacy rule rather
than a style one. An event payload reaches every listener, every log line and every queue row at
once — so it carries **ids and value objects only**. Never the aggregate itself, never a token, and
in this codebase never a CV body or a fragment of one (Constitution §8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """A past-tense fact. Immutable, because history is.

    Subclasses are named for what happened, in the past tense and in the language of the business:
    `BaseCvExtracted`, `TailoringCompleted`. A name like `CvUpdateEvent` describes a database
    operation instead of a business fact, and it is the first sign that a rule has escaped
    somewhere.
    """

    occurred_at: datetime


class RecordsEvents:
    """Mixin giving an aggregate a private buffer of events it has recorded.

    Composed in rather than inherited from a heavyweight `AggregateRoot` base class: aggregates in
    this codebase deliberately do not share a supertype (CLAUDE.md), because a shared *shape* is not
    shared *behaviour* and a base class ends up guessing at rules that differ between contexts.
    """

    __slots__ = ("_recorded_events",)

    _recorded_events: list[DomainEvent]

    def record(self, event: DomainEvent) -> None:
        """Buffer an event. Call this from inside the method that made the change, never outside."""
        if not hasattr(self, "_recorded_events"):
            self._recorded_events = []
        self._recorded_events.append(event)

    def release_events(self) -> tuple[DomainEvent, ...]:
        """Hand over the buffered events and empty the buffer.

        Emptying is the point: called twice, the second call returns nothing, so a retried handler
        cannot publish the same fact twice.
        """
        events = tuple(getattr(self, "_recorded_events", ()))
        self._recorded_events = []
        return events
