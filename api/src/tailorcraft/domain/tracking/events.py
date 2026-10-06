"""Domain events for the `tracking` bounded context.

An event's field set **is** a log field set — `LoggingEventPublisher` logs every field of every event
it receives — so these carry ids, stages and timestamps only (AC-6). Explicitly absent from all three:
the title, any posting text, title or URL, any CV label or filename, any email.

**There is no event for a retitle.** The only thing it could carry is the title (forbidden above) or
nothing, and an event that says "something changed" to no listener is noise. A stage, by contrast, is
a closed enum value and may reach a log line (OQ-19).

Pure data with no behaviour, so complete at the skeleton step (T3), like every other context's
`events.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.domain.tracking.value_objects import (
    ApplicationStage,
    TrackedApplicationId,
    TrackedRunRef,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationTracked(DomainEvent):
    """A user put one of their succeeded runs on the board.

    Payload: `tracked_application_id`, `user_id`, `tailoring_run_id`, `stage` (+ inherited
    `occurred_at`). `user_id` is a `UserId`, not an `Owner`: a card has no guest variant (plan §0.5).
    """

    tracked_application_id: TrackedApplicationId
    user_id: UserId
    tailoring_run_id: TrackedRunRef
    stage: ApplicationStage


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationStageChanged(DomainEvent):
    """A card moved from one stage to a **different** one. A move to the current stage is a no-op and
    records nothing (AC-4).

    Payload: `tracked_application_id`, `user_id`, `from_stage`, `to_stage` (+ `occurred_at`). Both
    stages, because "moved to rejected" means something different from `offer` than from `applied`.
    """

    tracked_application_id: TrackedApplicationId
    user_id: UserId
    from_stage: ApplicationStage
    to_stage: ApplicationStage


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationUntracked(DomainEvent):
    """A user took a card off the board. The run and its documents are untouched — only the card goes.

    Payload: `tracked_application_id`, `user_id` (+ `occurred_at`). The aggregate records it; the
    repository deletes the row; the use case publishes only if that deletion removed something.
    """

    tracked_application_id: TrackedApplicationId
    user_id: UserId
