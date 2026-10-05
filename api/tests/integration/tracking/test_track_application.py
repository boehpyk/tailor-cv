"""`TrackApplication` (slice 3.1, T11 RED, AC-8, spec failure-contract T-12…T-15, T-18).

Written from the acceptance criteria, not from the code: the use case is a skeleton at this point and
`__call__` raises `NotImplementedError`. Users, runs, postings and guest sessions are real rows
(`tailorcraft_test`, rolled back), reached through `GetTailoringRun` built over the real adapters; the
tracking repository is an in-memory double, because its adapter is T14's.

Every absence ("no row", "no event", "no run read") is paired with a positive control: the success
test asserts the same observation channel **does** record when the use case succeeds, so an assertion
that a skeleton satisfies by doing nothing is never the only evidence.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.tracking.track_application import (
    TrackApplication,
    TrackApplicationCommand,
)
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.tailoring.errors import (
    TailoringRunNotFound,
    TailoringRunNotOwnedByUser,
)
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.domain.tracking.errors import (
    ApplicationAlreadyTracked,
    TailoringRunNotTrackable,
    TooManyTrackedApplications,
)
from tailorcraft.domain.tracking.events import ApplicationTracked
from tailorcraft.domain.tracking.tracked_application import TrackedApplication
from tailorcraft.domain.tracking.value_objects import (
    ApplicationStage,
    ApplicationTitle,
    TrackedRunRef,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import FakeTrackedApplicationRepository, RecordingEventPublisher
from tests.integration.tracking.support import (
    SpyRuns,
    a_card,
    real_get_tailoring_run,
    real_users,
    seed_guest,
    seed_run,
    seed_user,
)


class _Env:
    def __init__(self, session: AsyncSession, clock: FixedClock, cap: int = 500) -> None:
        self.session = session
        self.clock = clock
        self.cards = FakeTrackedApplicationRepository()
        self.events = RecordingEventPublisher(self.cards)
        self.spy = SpyRuns(session)
        get_run = real_get_tailoring_run(session, self.spy, clock)
        self.use_case = TrackApplication(
            real_users(session), get_run, self.cards, self.events, clock, cap=cap
        )

    async def track(
        self,
        user_id: UserId,
        run_id: TailoringRunId,
        **kwargs: object,
    ) -> TrackedApplication:
        return await self.use_case(
            TrackApplicationCommand(user_id=user_id, tailoring_run_id=run_id, **kwargs)  # type: ignore[arg-type]
        )

    def assert_nothing_written(self) -> None:
        assert self.cards.added == []
        assert self.cards.all() == ()
        assert self.events.published == []


@pytest.fixture
def env(session: AsyncSession, clock: FixedClock) -> _Env:
    return _Env(session, clock)


# --- success -------------------------------------------------------------------------------------


async def test_tracking_a_succeeded_run_adds_one_card_in_to_apply_and_publishes_the_event(
    env: _Env,
) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    run = await seed_run(env.session, UserOwner(user_id), env.clock.now(), "succeeded")

    card = await env.track(user_id, run.id)

    assert card.user_id == user_id
    assert card.tailoring_run_id == TrackedRunRef(run.id.value)
    assert card.stage is ApplicationStage.TO_APPLY
    assert card.title is None
    assert card.version == 1
    assert card.tracked_at == env.clock.now()
    assert env.cards.added == [card]
    # Positive control for every "no run read" assertion below: a real track does read the run.
    assert env.spy.gets == [run.id]
    assert env.events.published == [
        ApplicationTracked(
            occurred_at=env.clock.now(),
            tracked_application_id=card.id,
            user_id=user_id,
            tailoring_run_id=TrackedRunRef(run.id.value),
            stage=ApplicationStage.TO_APPLY,
        )
    ]
    # Published after the write (the repo already held the card when the first event arrived).
    assert env.events.repo_size_at_first_publish == 1


async def test_tracking_with_a_stage_and_a_title_keeps_both(env: _Env) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    run = await seed_run(env.session, UserOwner(user_id), env.clock.now(), "succeeded")

    card = await env.track(
        user_id,
        run.id,
        stage=ApplicationStage.INTERVIEWING,
        title=ApplicationTitle("Acme — Senior Engineer"),
    )

    assert card.stage is ApplicationStage.INTERVIEWING
    assert card.title == ApplicationTitle("Acme — Senior Engineer")
    assert env.cards.added == [card]
    event = env.events.published[0]
    assert isinstance(event, ApplicationTracked)
    assert event.stage is ApplicationStage.INTERVIEWING


# --- the user is resolved first ------------------------------------------------------------------


async def test_an_erased_user_is_refused_before_any_run_or_card_is_read(env: _Env) -> None:
    owner_id = await seed_user(env.session, env.clock, "owner@example.com")
    run = await seed_run(env.session, UserOwner(owner_id), env.clock.now(), "succeeded")
    erased = UserId(value=uuid4())  # a valid token's subject whose row is gone

    with pytest.raises(UserNotFound):
        await env.track(erased, run.id)

    assert env.spy.gets == []
    assert env.cards.calls == []
    env.assert_nothing_written()


# --- the run is authorized by GetTailoringRun ----------------------------------------------------


async def test_a_nonexistent_run_is_tailoring_run_not_found_with_no_ownership_cause(
    env: _Env,
) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")

    with pytest.raises(TailoringRunNotFound) as caught:
        await env.track(user_id, TailoringRunId(value=uuid4()))

    assert not isinstance(caught.value.__cause__, TailoringRunNotOwnedByUser)
    assert env.spy.gets != []  # the run really was looked up
    env.assert_nothing_written()


async def test_another_users_run_is_tailoring_run_not_found_chained_from_not_owned_by_user(
    env: _Env,
) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    other_id = await seed_user(env.session, env.clock, "grace@example.com")
    run = await seed_run(env.session, UserOwner(other_id), env.clock.now(), "succeeded")

    with pytest.raises(TailoringRunNotFound) as caught:
        await env.track(user_id, run.id)

    assert isinstance(caught.value.__cause__, TailoringRunNotOwnedByUser)
    env.assert_nothing_written()


async def test_a_guests_run_is_tailoring_run_not_found_chained_from_not_owned_by_user(
    env: _Env,
) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    guest = await seed_guest(env.session, env.clock)
    run = await seed_run(env.session, guest, env.clock.now(), "succeeded")

    with pytest.raises(TailoringRunNotFound) as caught:
        await env.track(user_id, run.id)

    assert isinstance(caught.value.__cause__, TailoringRunNotOwnedByUser)
    env.assert_nothing_written()


# --- only a succeeded run is trackable -----------------------------------------------------------


@pytest.mark.parametrize("status", ["queued", "running", "failed"])
async def test_a_run_that_has_not_succeeded_is_not_trackable_and_carries_its_status(
    env: _Env, status: str
) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    run = await seed_run(env.session, UserOwner(user_id), env.clock.now(), status)

    with pytest.raises(TailoringRunNotTrackable) as caught:
        await env.track(user_id, run.id)

    assert caught.value.status == status
    assert env.spy.gets == [run.id]
    env.assert_nothing_written()


# --- one card per run ----------------------------------------------------------------------------


async def test_a_run_that_already_has_a_card_is_refused_naming_the_existing_card(
    env: _Env,
) -> None:
    user_id = await seed_user(env.session, env.clock, "ada@example.com")
    run = await seed_run(env.session, UserOwner(user_id), env.clock.now(), "succeeded")
    existing = a_card(user_id, env.clock.now(), run_id=run.id)
    env.cards.seed(existing)

    with pytest.raises(ApplicationAlreadyTracked) as caught:
        await env.track(user_id, run.id)

    assert caught.value.existing_id == existing.id
    assert env.cards.added == []
    assert env.cards.all() == (existing,)
    assert env.events.published == []


# --- the cap -------------------------------------------------------------------------------------


async def test_at_the_cap_a_new_track_is_refused_with_the_cap_and_writes_nothing(
    session: AsyncSession, clock: FixedClock
) -> None:
    env = _Env(session, clock, cap=2)
    user_id = await seed_user(session, clock, "ada@example.com")
    first = a_card(user_id, clock.now())
    second = a_card(user_id, clock.now())
    env.cards.seed(first)
    env.cards.seed(second)
    run = await seed_run(session, UserOwner(user_id), clock.now(), "succeeded")

    with pytest.raises(TooManyTrackedApplications) as caught:
        await env.track(user_id, run.id)

    assert caught.value.cap == 2
    assert env.cards.added == []
    assert set(env.cards.all()) == {first, second}
    assert env.events.published == []


async def test_one_below_the_cap_a_track_still_succeeds(
    session: AsyncSession, clock: FixedClock
) -> None:
    env = _Env(session, clock, cap=2)
    user_id = await seed_user(session, clock, "ada@example.com")
    env.cards.seed(a_card(user_id, clock.now()))
    run = await seed_run(session, UserOwner(user_id), clock.now(), "succeeded")

    card = await env.track(user_id, run.id)

    assert env.cards.added == [card]
    assert len(env.cards.all()) == 2


async def test_another_users_cards_do_not_count_against_the_cap(
    session: AsyncSession, clock: FixedClock
) -> None:
    env = _Env(session, clock, cap=2)
    user_id = await seed_user(session, clock, "ada@example.com")
    other_id = await seed_user(session, clock, "grace@example.com")
    env.cards.seed(a_card(other_id, clock.now()))
    env.cards.seed(a_card(other_id, clock.now()))
    run = await seed_run(session, UserOwner(user_id), clock.now(), "succeeded")

    card = await env.track(user_id, run.id)

    assert env.cards.added == [card]


async def test_an_already_tracked_run_is_reported_as_tracked_even_at_the_cap(
    session: AsyncSession, clock: FixedClock
) -> None:
    """Plan §2's order: the repeat check precedes the cap, so a double click on a full board is still
    answered with the card it already has (AC-38's client treats that as success)."""
    env = _Env(session, clock, cap=1)
    user_id = await seed_user(session, clock, "ada@example.com")
    run = await seed_run(session, UserOwner(user_id), clock.now(), "succeeded")
    existing = a_card(user_id, clock.now(), run_id=run.id)
    env.cards.seed(existing)

    with pytest.raises(ApplicationAlreadyTracked) as caught:
        await env.track(user_id, run.id)

    assert caught.value.existing_id == existing.id
