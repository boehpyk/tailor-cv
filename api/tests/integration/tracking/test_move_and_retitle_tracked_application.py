"""`MoveTrackedApplication` and `RetitleTrackedApplication` (slice 3.1, T11 RED, AC-9, AC-11,
failure-contract T-20…T-24).

The user is a real row (`tailorcraft_test`, rolled back) behind the real `UserRepository`; the card
repository is the in-memory double with a call log. "No `save`" is only ever asserted beside a
positive control that a real change **does** call it (AC-11's explicit demand), because a skeleton
that does nothing satisfies every absence.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.tracking.move_tracked_application import (
    MoveTrackedApplication,
    MoveTrackedApplicationCommand,
)
from tailorcraft.application.tracking.retitle_tracked_application import (
    RetitleTrackedApplication,
    RetitleTrackedApplicationCommand,
)
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.tracking.errors import (
    TrackedApplicationConcurrentlyModified,
    TrackedApplicationNotFound,
    TrackedApplicationNotOwnedByUser,
    TrackedApplicationVersionConflict,
)
from tailorcraft.domain.tracking.events import ApplicationStageChanged
from tailorcraft.domain.tracking.value_objects import (
    ApplicationStage,
    ApplicationTitle,
    TrackedApplicationId,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import FakeTrackedApplicationRepository, RecordingEventPublisher
from tests.integration.tracking.support import a_card, real_users, seed_user


class _Env:
    def __init__(
        self,
        session: AsyncSession,
        clock: FixedClock,
        cards: FakeTrackedApplicationRepository | None = None,
    ) -> None:
        self.session = session
        self.clock = clock
        self.cards = cards if cards is not None else FakeTrackedApplicationRepository()
        self.events = RecordingEventPublisher(self.cards)
        users = real_users(session)
        self.move = MoveTrackedApplication(users, self.cards, self.events, clock)
        self.retitle = RetitleTrackedApplication(users, self.cards, self.events, clock)


@pytest.fixture
def env(session: AsyncSession, clock: FixedClock) -> _Env:
    return _Env(session, clock)


# --- Move ----------------------------------------------------------------------------------------


async def test_moving_a_card_changes_its_stage_saves_once_and_publishes_the_change(
    env: _Env,
) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    card = a_card(user_id, env.clock.now())
    env.cards.seed(card)
    env.clock.advance(60)

    result = await env.move(
        MoveTrackedApplicationCommand(
            user_id=user_id, id=card.id, stage=ApplicationStage.APPLIED, expected_version=1
        )
    )

    assert result.stage is ApplicationStage.APPLIED
    assert result.version == 2
    assert result.stage_changed_at == env.clock.now()
    assert env.cards.saved == [result]
    assert env.events.published == [
        ApplicationStageChanged(
            occurred_at=env.clock.now(),
            tracked_application_id=card.id,
            user_id=user_id,
            from_stage=ApplicationStage.TO_APPLY,
            to_stage=ApplicationStage.APPLIED,
        )
    ]
    # Published after the save.
    assert env.cards.calls == ["get", "save"]
    assert env.events.repo_size_at_first_publish == 1


async def test_moving_to_the_current_stage_with_the_current_version_saves_and_publishes_nothing(
    env: _Env,
) -> None:
    """AC-11. Positive control: `test_moving_a_card_…` above shows a real move calls `save`."""
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    card = a_card(user_id, env.clock.now(), stage=ApplicationStage.OFFER)
    env.cards.seed(card)
    env.clock.advance(60)

    result = await env.move(
        MoveTrackedApplicationCommand(
            user_id=user_id, id=card.id, stage=ApplicationStage.OFFER, expected_version=1
        )
    )

    assert result is card
    assert result.stage is ApplicationStage.OFFER
    assert result.version == 1
    assert env.cards.calls == ["get"]  # it was loaded (the use case ran) and never saved
    assert env.cards.saved == []
    assert env.events.published == []


async def test_a_stale_version_is_a_conflict_even_when_the_stage_already_holds(
    env: _Env,
) -> None:
    """OQ-11: the version compare precedes the no-op rule, so a stale tab is told it is stale."""
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    card = a_card(user_id, env.clock.now(), stage=ApplicationStage.APPLIED)
    env.cards.seed(card)

    with pytest.raises(TrackedApplicationVersionConflict) as caught:
        await env.move(
            MoveTrackedApplicationCommand(
                user_id=user_id, id=card.id, stage=ApplicationStage.APPLIED, expected_version=7
            )
        )

    assert caught.value.expected_version == 7
    assert caught.value.current_version == 1
    assert env.cards.calls == ["get"]
    assert env.cards.saved == []
    assert env.events.published == []


async def test_a_save_that_finds_the_row_changed_propagates_concurrently_modified(
    session: AsyncSession, clock: FixedClock
) -> None:
    cards = FakeTrackedApplicationRepository(conflict_on_save=1)
    env = _Env(session, clock, cards)
    user_id = await seed_user(session, clock, "ada@example.com")
    card = a_card(user_id, clock.now())
    cards.seed(card)

    with pytest.raises(TrackedApplicationConcurrentlyModified):
        await env.move(
            MoveTrackedApplicationCommand(
                user_id=user_id, id=card.id, stage=ApplicationStage.APPLIED, expected_version=1
            )
        )

    assert cards.calls == ["get", "save"]  # the conflict really came from the save
    assert env.events.published == []  # the lost write announces nothing


async def test_moving_a_nonexistent_card_is_not_found_without_an_ownership_cause(
    env: _Env,
) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")

    with pytest.raises(TrackedApplicationNotFound) as caught:
        await env.move(
            MoveTrackedApplicationCommand(
                user_id=user_id,
                id=TrackedApplicationId(value=uuid4()),
                stage=ApplicationStage.APPLIED,
                expected_version=1,
            )
        )

    assert not isinstance(caught.value.__cause__, TrackedApplicationNotOwnedByUser)
    assert env.cards.calls == ["get"]
    assert env.cards.saved == []
    assert env.events.published == []


async def test_moving_another_users_card_is_not_found_chained_from_not_owned_and_changes_nothing(
    env: _Env,
) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    other_id = await seed_user(env.session, env.clock, "grace@example.com")
    card = a_card(other_id, env.clock.now())
    env.cards.seed(card)

    with pytest.raises(TrackedApplicationNotFound) as caught:
        await env.move(
            MoveTrackedApplicationCommand(
                user_id=user_id, id=card.id, stage=ApplicationStage.OFFER, expected_version=1
            )
        )

    assert isinstance(caught.value.__cause__, TrackedApplicationNotOwnedByUser)
    assert env.cards.calls == ["get"]  # the card was read (so the refusal is the equality's)
    assert env.cards.saved == []
    assert card.stage is ApplicationStage.TO_APPLY
    assert card.version == 1
    assert env.events.published == []


async def test_moving_for_an_erased_user_is_refused_before_the_card_is_read(env: _Env) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    card = a_card(user_id, env.clock.now())
    env.cards.seed(card)

    with pytest.raises(UserNotFound):
        await env.move(
            MoveTrackedApplicationCommand(
                user_id=UserId(value=uuid4()),
                id=card.id,
                stage=ApplicationStage.APPLIED,
                expected_version=1,
            )
        )

    assert env.cards.calls == []
    assert env.events.published == []
    assert card.stage is ApplicationStage.TO_APPLY


# --- Retitle -------------------------------------------------------------------------------------


async def test_retitling_sets_the_title_bumps_the_version_saves_once_and_publishes_no_event(
    env: _Env,
) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    card = a_card(user_id, env.clock.now())
    env.cards.seed(card)
    env.clock.advance(60)

    result = await env.retitle(
        RetitleTrackedApplicationCommand(
            user_id=user_id,
            id=card.id,
            title=ApplicationTitle("Acme — Staff"),
            expected_version=1,
        )
    )

    assert result.title == ApplicationTitle("Acme — Staff")
    assert result.version == 2
    assert result.stage_changed_at == card.tracked_at  # a retitle is not a stage change
    assert env.cards.calls == ["get", "save"]
    assert env.cards.saved == [result]
    assert env.events.published == []  # a title change is not a fact anyone listens for


async def test_retitling_to_none_clears_the_title(env: _Env) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    card = a_card(user_id, env.clock.now(), title=ApplicationTitle("Old"))
    env.cards.seed(card)

    result = await env.retitle(
        RetitleTrackedApplicationCommand(
            user_id=user_id, id=card.id, title=None, expected_version=1
        )
    )

    assert result.title is None
    assert result.version == 2
    assert env.cards.saved == [result]


async def test_retitling_to_the_same_title_with_the_current_version_does_not_save(
    env: _Env,
) -> None:
    """AC-11. Positive control: the retitle test above shows a real change calls `save`."""
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    card = a_card(user_id, env.clock.now(), title=ApplicationTitle("Same"))
    env.cards.seed(card)

    result = await env.retitle(
        RetitleTrackedApplicationCommand(
            user_id=user_id, id=card.id, title=ApplicationTitle("Same"), expected_version=1
        )
    )

    assert result is card
    assert result.version == 1
    assert env.cards.calls == ["get"]
    assert env.cards.saved == []
    assert env.events.published == []


async def test_retitling_with_a_stale_version_is_a_conflict_and_saves_nothing(env: _Env) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    card = a_card(user_id, env.clock.now(), title=ApplicationTitle("Same"))
    env.cards.seed(card)

    with pytest.raises(TrackedApplicationVersionConflict) as caught:
        await env.retitle(
            RetitleTrackedApplicationCommand(
                user_id=user_id, id=card.id, title=ApplicationTitle("Same"), expected_version=3
            )
        )

    assert (caught.value.expected_version, caught.value.current_version) == (3, 1)
    assert env.cards.calls == ["get"]
    assert env.cards.saved == []


async def test_a_retitle_save_that_finds_the_row_changed_propagates_concurrently_modified(
    session: AsyncSession, clock: FixedClock
) -> None:
    cards = FakeTrackedApplicationRepository(conflict_on_save=1)
    env = _Env(session, clock, cards)
    user_id = await seed_user(session, clock, "ada@example.com")
    card = a_card(user_id, clock.now())
    cards.seed(card)

    with pytest.raises(TrackedApplicationConcurrentlyModified):
        await env.retitle(
            RetitleTrackedApplicationCommand(
                user_id=user_id, id=card.id, title=ApplicationTitle("New"), expected_version=1
            )
        )

    assert cards.calls == ["get", "save"]


async def test_retitling_a_nonexistent_card_is_not_found_without_an_ownership_cause(
    env: _Env,
) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")

    with pytest.raises(TrackedApplicationNotFound) as caught:
        await env.retitle(
            RetitleTrackedApplicationCommand(
                user_id=user_id,
                id=TrackedApplicationId(value=uuid4()),
                title=ApplicationTitle("x"),
                expected_version=1,
            )
        )

    assert not isinstance(caught.value.__cause__, TrackedApplicationNotOwnedByUser)
    assert env.cards.calls == ["get"]
    assert env.cards.saved == []


async def test_retitling_another_users_card_is_not_found_chained_from_not_owned(
    env: _Env,
) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    other_id = await seed_user(env.session, env.clock, "grace@example.com")
    card = a_card(other_id, env.clock.now(), title=ApplicationTitle("Theirs"))
    env.cards.seed(card)

    with pytest.raises(TrackedApplicationNotFound) as caught:
        await env.retitle(
            RetitleTrackedApplicationCommand(
                user_id=user_id, id=card.id, title=ApplicationTitle("Mine"), expected_version=1
            )
        )

    assert isinstance(caught.value.__cause__, TrackedApplicationNotOwnedByUser)
    assert env.cards.calls == ["get"]
    assert env.cards.saved == []
    assert card.title == ApplicationTitle("Theirs")
    assert card.version == 1


async def test_retitling_for_an_erased_user_is_refused_before_the_card_is_read(
    env: _Env,
) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    card = a_card(user_id, env.clock.now())
    env.cards.seed(card)

    with pytest.raises(UserNotFound):
        await env.retitle(
            RetitleTrackedApplicationCommand(
                user_id=UserId(value=uuid4()),
                id=card.id,
                title=ApplicationTitle("x"),
                expected_version=1,
            )
        )

    assert env.cards.calls == []
    assert card.title is None
