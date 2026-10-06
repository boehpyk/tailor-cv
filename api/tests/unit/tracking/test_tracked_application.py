"""AC-3, AC-4, AC-5, AC-6: the `TrackedApplication` aggregate.

Pure domain tests: no I/O, no fixtures, no event loop, no mocks, and no `datetime.now()` -- every
instant is a literal whole-second UTC datetime. Written from the spec (AC-3...AC-6, TA-1...TA-5),
against the T3 skeleton whose `track`/`move_to`/`retitle`/`untrack` and properties raise
`NotImplementedError`. `NotImplementedError` subclasses `RuntimeError`, so no test here expects a
`RuntimeError`; refusals assert the exact domain type.

Absence assertions ("no event", "version unchanged") are each paired with a discriminating positive
(the effective cell beside the no-op cell) in the same test or its twin, so an implementation that
simply never records anything cannot satisfy both.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.domain.tracking.errors import TrackedApplicationVersionConflict
from tailorcraft.domain.tracking.events import (
    ApplicationStageChanged,
    ApplicationTracked,
    ApplicationUntracked,
)
from tailorcraft.domain.tracking.tracked_application import TrackedApplication
from tailorcraft.domain.tracking.value_objects import (
    ApplicationStage,
    ApplicationTitle,
    TrackedApplicationId,
    TrackedRunRef,
)

T0 = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
T2 = T0 + timedelta(minutes=10)

CARD_ID = TrackedApplicationId(UUID("00000000-0000-7000-8000-000000000001"))
USER_ID = UserId(UUID("00000000-0000-7000-8000-0000000000aa"))
RUN_ID = TrackedRunRef(UUID("00000000-0000-7000-8000-0000000000bb"))

ALL_STAGES = list(ApplicationStage)


def _track(
    stage: ApplicationStage = ApplicationStage.TO_APPLY,
    title: ApplicationTitle | None = None,
    at: datetime = T0,
) -> TrackedApplication:
    return TrackedApplication.track(
        id=CARD_ID,
        user_id=USER_ID,
        tailoring_run_id=RUN_ID,
        stage=stage,
        title=title,
        at=at,
    )


def _tracked_card(
    stage: ApplicationStage = ApplicationStage.TO_APPLY,
    title: ApplicationTitle | None = None,
) -> TrackedApplication:
    """A card as a use case would load it: tracked, with its creation event already released."""
    card = _track(stage, title)
    card.release_events()
    return card


# ---- AC-3: track ------------------------------------------------------------------------------


def test_track_starts_at_version_one() -> None:
    assert _track().version == 1


def test_track_keeps_identity_user_run_stage_and_title() -> None:
    title = ApplicationTitle("Staff Engineer at Acme")

    card = _track(stage=ApplicationStage.APPLIED, title=title)

    assert card.id == CARD_ID
    assert card.user_id == USER_ID
    assert card.tailoring_run_id == RUN_ID
    assert card.stage is ApplicationStage.APPLIED
    assert card.title == title


def test_track_without_a_title_has_no_title() -> None:
    assert _track(title=None).title is None


def test_track_sets_both_timestamps_to_at() -> None:
    card = _track(at=T1)

    assert card.tracked_at == T1
    assert card.stage_changed_at == T1


def test_track_records_exactly_one_application_tracked_event() -> None:
    card = _track(stage=ApplicationStage.INTERVIEWING, at=T1)

    events = card.release_events()

    assert events == (
        ApplicationTracked(
            occurred_at=T1,
            tracked_application_id=CARD_ID,
            user_id=USER_ID,
            tailoring_run_id=RUN_ID,
            stage=ApplicationStage.INTERVIEWING,
        ),
    )


def test_tracked_event_carries_no_title_even_when_the_card_has_one() -> None:
    card = _track(title=ApplicationTitle("SECRETMARKER"))

    (event,) = card.release_events()

    assert "SECRETMARKER" not in repr(event)


def test_releasing_events_twice_returns_nothing_the_second_time() -> None:
    card = _track()

    assert len(card.release_events()) == 1
    assert card.release_events() == ()


def test_track_with_a_naive_at_is_an_invariant_violation() -> None:
    with pytest.raises(InvariantViolated):
        _track(at=datetime(2026, 10, 5, 12, 0, 0))


def test_track_with_a_sub_second_at_is_an_invariant_violation() -> None:
    with pytest.raises(InvariantViolated):
        _track(at=T0.replace(microsecond=1))


def test_track_accepts_a_whole_second_at_in_a_non_utc_zone() -> None:
    # The positive twin of the two refusals above: only naive and sub-second are refused.
    at = datetime(2026, 10, 5, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))

    assert _track(at=at).tracked_at == at


def test_a_card_cannot_be_built_with_its_attributes_as_arguments() -> None:
    """`track` is the only constructor: a mapped class's default constructor would accept the
    mapped attribute names (CLAUDE.md), so the explicit no-argument `__init__` must refuse them."""
    with pytest.raises(TypeError):
        TrackedApplication(_stage=ApplicationStage.OFFER)  # type: ignore[call-arg]


# ---- AC-4: move_to -- the 30 effective cells and the 6 no-op cells ----------------------------

_EFFECTIVE = [(a, b) for a in ALL_STAGES for b in ALL_STAGES if a is not b]
_NOOP = [(a, a) for a in ALL_STAGES]


def test_the_move_table_has_thirty_effective_cells_and_six_no_op_cells() -> None:
    assert len(_EFFECTIVE) == 30
    assert len(_NOOP) == 6


@pytest.mark.parametrize(("source", "target"), _EFFECTIVE, ids=lambda s: s.value)
def test_a_card_may_move_from_any_stage_to_any_other(
    source: ApplicationStage, target: ApplicationStage
) -> None:
    card = _tracked_card(source)

    card.move_to(target, expected_version=1, at=T1)

    assert card.stage is target
    assert card.stage_changed_at == T1
    assert card.version == 2
    assert card.tracked_at == T0
    assert card.release_events() == (
        ApplicationStageChanged(
            occurred_at=T1,
            tracked_application_id=CARD_ID,
            user_id=USER_ID,
            from_stage=source,
            to_stage=target,
        ),
    )


@pytest.mark.parametrize(("source", "target"), _NOOP, ids=lambda s: s.value)
def test_moving_to_the_current_stage_changes_nothing_and_records_nothing(
    source: ApplicationStage, target: ApplicationStage
) -> None:
    card = _tracked_card(source)

    card.move_to(target, expected_version=1, at=T1)

    assert card.stage is source
    assert card.stage_changed_at == T0
    assert card.version == 1
    assert card.release_events() == ()


def test_a_no_op_followed_by_a_real_move_still_bumps_only_once() -> None:
    """Discriminating positive for the no-op cells: the card is live and does record when it moves."""
    card = _tracked_card(ApplicationStage.APPLIED)

    card.move_to(ApplicationStage.APPLIED, expected_version=1, at=T1)
    card.move_to(ApplicationStage.OFFER, expected_version=1, at=T1)

    assert card.version == 2
    assert len(card.release_events()) == 1


def test_consecutive_moves_each_bump_the_version_by_one() -> None:
    card = _tracked_card()

    card.move_to(ApplicationStage.APPLIED, expected_version=1, at=T1)
    card.move_to(ApplicationStage.INTERVIEWING, expected_version=2, at=T2)

    assert card.version == 3
    assert card.stage_changed_at == T2
    assert [e.to_stage for e in card.release_events()] == [  # type: ignore[attr-defined]
        ApplicationStage.APPLIED,
        ApplicationStage.INTERVIEWING,
    ]


def test_a_card_can_leave_a_terminal_looking_stage_again() -> None:
    """Belief is corrected (TA-2): `rejected` is not terminal."""
    card = _tracked_card(ApplicationStage.REJECTED)

    card.move_to(ApplicationStage.INTERVIEWING, expected_version=1, at=T1)

    assert card.stage is ApplicationStage.INTERVIEWING


# ---- AC-4: version check -- before the no-op rule ----------------------------------------------


def test_a_stale_version_on_a_real_move_is_a_conflict_and_changes_nothing() -> None:
    card = _tracked_card(ApplicationStage.TO_APPLY)

    with pytest.raises(TrackedApplicationVersionConflict) as excinfo:
        card.move_to(ApplicationStage.APPLIED, expected_version=7, at=T1)

    assert excinfo.value.expected_version == 7
    assert excinfo.value.current_version == 1
    assert card.stage is ApplicationStage.TO_APPLY
    assert card.version == 1
    assert card.stage_changed_at == T0
    assert card.release_events() == ()


def test_the_version_check_comes_before_the_no_op_check() -> None:
    """OQ-11: a stale tab is told it is stale even when the stage it wants already holds."""
    card = _tracked_card(ApplicationStage.APPLIED)

    with pytest.raises(TrackedApplicationVersionConflict) as excinfo:
        card.move_to(ApplicationStage.APPLIED, expected_version=0, at=T1)

    assert excinfo.value.expected_version == 0
    assert excinfo.value.current_version == 1


def test_the_version_check_comes_before_the_time_check() -> None:
    card = _tracked_card(ApplicationStage.APPLIED)

    with pytest.raises(TrackedApplicationVersionConflict):
        card.move_to(ApplicationStage.OFFER, expected_version=9, at=T0 - timedelta(hours=1))


# ---- AC-4 / TA-3: time -------------------------------------------------------------------------


def test_a_move_with_an_at_earlier_than_stage_changed_at_is_an_invariant_violation() -> None:
    card = _tracked_card()
    card.move_to(ApplicationStage.APPLIED, expected_version=1, at=T2)
    card.release_events()

    with pytest.raises(InvariantViolated):
        card.move_to(ApplicationStage.OFFER, expected_version=2, at=T1)

    assert card.stage is ApplicationStage.APPLIED
    assert card.version == 2
    assert card.stage_changed_at == T2
    assert card.release_events() == ()


def test_a_move_at_exactly_stage_changed_at_is_allowed() -> None:
    """Positive twin: `never runs backwards` means strictly earlier is refused, equal is not."""
    card = _tracked_card()

    card.move_to(ApplicationStage.APPLIED, expected_version=1, at=T0)

    assert card.stage_changed_at == T0
    assert card.version == 2


def test_a_move_with_a_naive_at_is_an_invariant_violation() -> None:
    card = _tracked_card()

    with pytest.raises(InvariantViolated):
        card.move_to(
            ApplicationStage.APPLIED, expected_version=1, at=datetime(2026, 10, 5, 12, 5, 0)
        )

    assert card.stage is ApplicationStage.TO_APPLY
    assert card.version == 1


def test_a_move_with_a_sub_second_at_is_an_invariant_violation() -> None:
    card = _tracked_card()

    with pytest.raises(InvariantViolated):
        card.move_to(
            ApplicationStage.APPLIED, expected_version=1, at=T1.replace(microsecond=500_000)
        )

    assert card.stage is ApplicationStage.TO_APPLY
    assert card.version == 1


# ---- AC-5: retitle -----------------------------------------------------------------------------


def test_retitle_sets_a_title_bumps_the_version_and_records_no_event() -> None:
    card = _tracked_card()

    card.retitle(ApplicationTitle("Staff Engineer"), expected_version=1, at=T1)

    assert card.title == ApplicationTitle("Staff Engineer")
    assert card.version == 2
    assert card.release_events() == ()


def test_retitle_with_none_clears_the_title() -> None:
    card = _tracked_card(title=ApplicationTitle("Staff Engineer"))

    card.retitle(None, expected_version=1, at=T1)

    assert card.title is None
    assert card.version == 2


def test_retitle_replaces_one_title_with_another() -> None:
    card = _tracked_card(title=ApplicationTitle("Old"))

    card.retitle(ApplicationTitle("New"), expected_version=1, at=T1)

    assert card.title == ApplicationTitle("New")
    assert card.version == 2


def test_retitle_does_not_touch_stage_or_stage_changed_at() -> None:
    card = _tracked_card(ApplicationStage.INTERVIEWING)

    card.retitle(ApplicationTitle("Renamed"), expected_version=1, at=T2)

    assert card.stage is ApplicationStage.INTERVIEWING
    assert card.stage_changed_at == T0
    assert card.tracked_at == T0


def test_retitle_to_the_same_title_is_a_no_op() -> None:
    card = _tracked_card(title=ApplicationTitle("Same"))

    card.retitle(ApplicationTitle("Same"), expected_version=1, at=T1)

    assert card.title == ApplicationTitle("Same")
    assert card.version == 1


def test_retitle_none_on_an_untitled_card_is_a_no_op() -> None:
    card = _tracked_card(title=None)

    card.retitle(None, expected_version=1, at=T1)

    assert card.title is None
    assert card.version == 1


def test_a_no_op_retitle_does_not_consume_a_version_a_real_one_would() -> None:
    """Discriminating positive for the no-op: after it, version 1 is still the right token."""
    card = _tracked_card(title=ApplicationTitle("Same"))

    card.retitle(ApplicationTitle("Same"), expected_version=1, at=T1)
    card.retitle(ApplicationTitle("Different"), expected_version=1, at=T1)

    assert card.version == 2
    assert card.title == ApplicationTitle("Different")


def test_retitle_with_a_stale_version_is_a_conflict_and_changes_nothing() -> None:
    card = _tracked_card(title=ApplicationTitle("Old"))

    with pytest.raises(TrackedApplicationVersionConflict) as excinfo:
        card.retitle(ApplicationTitle("New"), expected_version=3, at=T1)

    assert excinfo.value.expected_version == 3
    assert excinfo.value.current_version == 1
    assert card.title == ApplicationTitle("Old")
    assert card.version == 1


def test_retitle_checks_the_version_before_the_no_op_rule() -> None:
    card = _tracked_card(title=ApplicationTitle("Same"))

    with pytest.raises(TrackedApplicationVersionConflict):
        card.retitle(ApplicationTitle("Same"), expected_version=2, at=T1)


def test_a_retitle_then_a_move_use_consecutive_versions() -> None:
    card = _tracked_card()

    card.retitle(ApplicationTitle("Named"), expected_version=1, at=T1)
    card.move_to(ApplicationStage.APPLIED, expected_version=2, at=T2)

    assert card.version == 3
    assert card.stage_changed_at == T2
    assert len(card.release_events()) == 1


# ---- AC-6: untrack -----------------------------------------------------------------------------


def test_untrack_records_exactly_one_application_untracked_event() -> None:
    card = _tracked_card(ApplicationStage.OFFER, title=ApplicationTitle("SECRETMARKER"))

    card.untrack(T1)

    events = card.release_events()
    assert events == (
        ApplicationUntracked(occurred_at=T1, tracked_application_id=CARD_ID, user_id=USER_ID),
    )
    assert "SECRETMARKER" not in repr(events)


def test_untrack_changes_no_state() -> None:
    card = _tracked_card(ApplicationStage.OFFER)

    card.untrack(T1)

    assert card.stage is ApplicationStage.OFFER
    assert card.version == 1
    assert card.stage_changed_at == T0
