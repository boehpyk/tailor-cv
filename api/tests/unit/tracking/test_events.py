"""AC-6: the three tracking events' exact field sets.

`LoggingEventPublisher` logs every field of every event, so an event's field set *is* a log field
set. Checked structurally with `dataclasses.fields()` (as `tests/unit/identity/test_events.py`
does), so a field nobody predicted -- `title`, `posting_title`, `email` -- fails by being absent
from the expected set rather than by a string grep that passes vacuously.

`events.py` is complete in the skeleton, so these are green on arrival and guard T5's GREEN.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.domain.tracking.events import (
    ApplicationStageChanged,
    ApplicationTracked,
    ApplicationUntracked,
)
from tailorcraft.domain.tracking.value_objects import (
    ApplicationStage,
    TrackedApplicationId,
    TrackedRunRef,
)


def _names(event_type: type) -> set[str]:
    return {f.name for f in dataclasses.fields(event_type)}


def test_application_tracked_field_set_is_exactly_the_agreed_fields() -> None:
    assert _names(ApplicationTracked) == {
        "occurred_at",
        "tracked_application_id",
        "user_id",
        "tailoring_run_id",
        "stage",
    }


def test_application_stage_changed_field_set_is_exactly_the_agreed_fields() -> None:
    assert _names(ApplicationStageChanged) == {
        "occurred_at",
        "tracked_application_id",
        "user_id",
        "from_stage",
        "to_stage",
    }


def test_application_untracked_field_set_is_exactly_the_agreed_fields() -> None:
    assert _names(ApplicationUntracked) == {
        "occurred_at",
        "tracked_application_id",
        "user_id",
    }


@pytest.mark.parametrize(
    "event_type", [ApplicationTracked, ApplicationStageChanged, ApplicationUntracked]
)
def test_every_tracking_event_is_a_frozen_slotted_domain_event(event_type: type) -> None:
    assert issubclass(event_type, DomainEvent)
    assert event_type.__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert hasattr(event_type, "__slots__")


def test_events_carry_the_typed_ids_not_bare_uuids() -> None:
    at = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
    event = ApplicationTracked(
        occurred_at=at,
        tracked_application_id=TrackedApplicationId(uuid4()),
        user_id=UserId(uuid4()),
        tailoring_run_id=TrackedRunRef(uuid4()),
        stage=ApplicationStage.APPLIED,
    )

    assert isinstance(event.tracked_application_id, TrackedApplicationId)
    assert isinstance(event.user_id, UserId)
    assert isinstance(event.tailoring_run_id, TrackedRunRef)
    assert event.stage is ApplicationStage.APPLIED
