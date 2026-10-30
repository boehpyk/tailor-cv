"""Application tests for `RequestFitScore` (slice 5.1, T15, RED): count, reserve, call, store, settle.

Written from the feature spec's AC-4, 8, 10, 12, 13, 14, 21 and failure contract rows S-1 to S-10, S-14
to S-16 and S-25 to S-27, and the technical plan's § 2 with its *Corrections* section, never from the
skeleton. Against fakes, as `RequestTailoringRun`'s tests are: the ports are real `Protocol`
implementations, the aggregates and value objects are the real domain classes.

**The ledger is the receipt for money and the order is the transaction boundary** (ADR-0041): each
fake appends to one `journal`, so "reserved before the call, settled after it" is a list equality.
Every rejecting test also asserts nothing was stored and no event published, because a use case that
"helpfully" recorded a failure would still raise the right class.

Money is asserted against the hand-computed constants in `support.py`, never against `ScoringPrices`.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tailorcraft.domain.identity.errors import GuestSessionExpired, UserNotFound
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.posting.errors import JobPostingNotFound
from tailorcraft.domain.scoring.errors import (
    FitScoreSpendCapReached,
    FitScoringFailed,
    FitScoringOutputInvalid,
    TooManyFitScores,
)
from tailorcraft.domain.scoring.events import FitScoreRecorded
from tailorcraft.domain.scoring.value_objects import (
    CvForScoring,
    FitScoreFailureReason,
    MicroUsd,
    PostingForScoring,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.scoring.support import (
    DAY,
    MAX_PER_SESSION,
    MAX_PER_USER,
    RESERVED,
    USUAL_COST,
    WORST_ATTEMPT,
    LedgerDown,
    StoreDown,
    World,
    a_guest_owner,
    a_stored_score,
    assessed,
    pair_of,
    usage,
)

# --- The happy path and its order -------------------------------------------------------------------


async def test_a_new_pair_is_stored_with_the_assessment_the_cost_and_the_instant(
    clock: FixedClock,
) -> None:
    w = World(clock)
    guest = await w.guest()
    pair = pair_of()

    outcome = await w.ask(guest, pair)

    assert outcome.created is True
    stored = outcome.stored
    assert w.scores.all() == [stored]
    assert stored.owner == guest
    assert stored.base_cv_id == pair.cv_id
    assert stored.job_posting_id == pair.posting_id
    assert stored.cv_fingerprint == pair.cv_fingerprint
    assert stored.posting_fingerprint == pair.posting_fingerprint
    assert stored.assessment == w.outcome.assessment  # type: ignore[union-attr]
    assert stored.usage == w.outcome.usage
    assert stored.cost == MicroUsd(USUAL_COST)
    assert stored.scored_at == clock.now()


async def test_the_calls_are_made_in_the_order_that_makes_the_ledger_a_receipt(
    clock: FixedClock,
) -> None:
    """Count, reserve (commits), assess with nothing held, store, settle (commits the row and its
    cost together), and only then the event (AC-13, AC-14, plan § 2)."""
    w = World(clock)

    await w.ask(await w.guest(), pair_of())

    assert w.journal == ["scored_at_since", "reserve", "assess", "store", "settle", "publish"]


async def test_the_reservation_is_already_spent_and_nothing_is_stored_when_the_model_is_asked(
    clock: FixedClock,
) -> None:
    w = World(clock)

    await w.ask(await w.guest(), pair_of())

    assert w.at_assess["spent"] == {w.day: RESERVED}
    assert w.at_assess["settlements"] == []
    assert w.at_assess["rows"] == []


async def test_the_model_is_asked_about_exactly_the_pairs_two_texts(clock: FixedClock) -> None:
    w = World(clock)
    pair = pair_of()

    await w.ask(await w.guest(), pair)

    assert w.scorer.calls == [(pair.cv, pair.posting)]


async def test_one_event_is_published_for_the_stored_score_after_the_settlement(
    clock: FixedClock,
) -> None:
    w = World(clock)

    outcome = await w.ask(await w.guest(), pair_of())

    assert len(w.events.published) == 1
    event = w.events.published[0]
    assert isinstance(event, FitScoreRecorded)
    assert event.fit_score_id == outcome.stored.id
    assert event.cost_microusd == USUAL_COST
    assert event.gap_count == 2
    assert w.journal[-2:] == ["settle", "publish"]


async def test_the_clock_is_read_once(clock: FixedClock) -> None:
    w = World(clock)

    await w.ask(await w.guest(), pair_of())

    assert w.clock.calls == 1


async def test_a_call_across_midnight_settles_the_day_it_reserved_and_stamps_the_instant_it_began(
    clock: FixedClock,
) -> None:
    late = FixedClock(datetime(2026, 9, 4, 23, 59, 30, tzinfo=UTC))
    w = World(late, on_assess=lambda: late.advance(120))

    outcome = await w.ask(await w.guest(), pair_of())

    first_day = datetime(2026, 9, 4, tzinfo=UTC).date()
    assert [r[0] for r in w.ledger.reservations] == [first_day]
    assert [s[0] for s in w.ledger.settlements] == [first_day]
    assert w.ledger.spent == {first_day: USUAL_COST}
    assert outcome.stored.scored_at == datetime(2026, 9, 4, 23, 59, 30, tzinfo=UTC)


async def test_scoring_does_not_hash_on_the_event_loop(
    clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pair carries its fingerprints (technical-plan corrections, T14), so a request reads
    `.fingerprint` of neither text: a 250,000-character CV is not hashed again on the loop (AC-22)."""
    w = World(clock)
    guest = await w.guest()
    pair = pair_of()
    reads: list[int] = []

    def spying(original: Callable[[Any], Any]) -> property:
        def fingerprint(self: Any) -> Any:
            reads.append(threading.get_ident())
            return original(self)

        return property(fingerprint)

    for cls in (CvForScoring, PostingForScoring):
        monkeypatch.setattr(cls, "fingerprint", spying(vars(cls)["fingerprint"].fget))

    await w.ask(guest, pair)

    assert reads == []


# --- AC-4: no score crosses owners --------------------------------------------------------------------


async def test_the_same_pair_for_two_owners_is_two_calls_and_two_rows(clock: FixedClock) -> None:
    w = World(clock)
    alice, bob = await w.guest("alice"), await w.guest("bob")
    pair = pair_of()

    first = await w.ask(alice, pair)
    second = await w.ask(bob, pair)

    assert first.created is True
    assert second.created is True
    assert first.stored.id != second.stored.id
    assert len(w.scorer.calls) == 2
    assert {row.owner for row in w.scores.all()} == {alice, bob}


# --- AC-8, AC-12: the limits -------------------------------------------------------------------------------


async def test_a_guest_at_the_limit_is_refused_with_no_reservation_call_or_row(
    clock: FixedClock,
) -> None:
    w = World(clock)
    guest = await w.guest()
    w.seed_scores(guest, [clock.now() - timedelta(hours=h) for h in (1, 2, 3)])

    with pytest.raises(TooManyFitScores) as info:
        await w.ask(guest, pair_of())

    assert (info.value.count, info.value.limit) == (MAX_PER_SESSION, MAX_PER_SESSION)
    assert info.value.retry_not_before is None
    assert w.journal == ["scored_at_since"]
    assert w.scorer.calls == []
    assert w.ledger.reservations == []
    assert len(w.scores.all()) == 3
    assert w.events.published == []


async def test_a_guest_one_below_the_limit_is_scored(clock: FixedClock) -> None:
    w = World(clock)
    guest = await w.guest()
    w.seed_scores(guest, [clock.now() - timedelta(hours=h) for h in (1, 2)])

    outcome = await w.ask(guest, pair_of())

    assert outcome.created is True
    assert len(w.scores.all()) == 3


async def test_a_guests_count_has_no_window_so_old_scores_still_count(clock: FixedClock) -> None:
    w = World(clock)
    guest = await w.guest()
    w.seed_scores(guest, [clock.now() - timedelta(hours=h) for h in (30, 40, 50)])

    with pytest.raises(TooManyFitScores):
        await w.ask(guest, pair_of())

    assert w.scores.since_calls == [(guest, None)]


async def test_another_owners_scores_do_not_count_against_a_guest(clock: FixedClock) -> None:
    w = World(clock)
    guest, other = await w.guest("one"), await w.guest("two")
    w.seed_scores(other, [clock.now() - timedelta(hours=h) for h in (1, 2, 3)])

    outcome = await w.ask(guest, pair_of())

    assert outcome.created is True


async def test_a_user_is_held_to_the_users_limit_not_the_guests(clock: FixedClock) -> None:
    w = World(clock)
    user = await w.user()
    w.seed_scores(user, [clock.now() - timedelta(hours=h) for h in range(1, MAX_PER_SESSION + 3)])

    outcome = await w.ask(user, pair_of())

    assert outcome.created is True


async def test_a_users_window_is_the_last_24_hours(clock: FixedClock) -> None:
    w = World(clock)
    user = await w.user()

    await w.ask(user, pair_of())

    assert w.scores.since_calls == [(user, clock.now() - DAY)]


async def test_a_user_at_the_limit_is_told_when_the_oldest_score_stops_counting(
    clock: FixedClock,
) -> None:
    w = World(clock)
    user = await w.user()
    oldest = clock.now() - timedelta(hours=23)
    w.seed_scores(user, [oldest + timedelta(minutes=m) for m in range(MAX_PER_USER)])

    with pytest.raises(TooManyFitScores) as info:
        await w.ask(user, pair_of())

    assert (info.value.count, info.value.limit) == (MAX_PER_USER, MAX_PER_USER)
    assert info.value.retry_not_before == oldest + DAY
    assert w.journal == ["scored_at_since"]
    assert w.scorer.calls == []
    assert w.ledger.reservations == []


async def test_a_user_whose_oldest_score_is_exactly_24_hours_old_is_admitted(
    clock: FixedClock,
) -> None:
    """`scored_at_since` is exclusive, so at exactly `retry_not_before` the oldest has stopped
    counting: a user returning at the second they were told is let in (technical-plan, T14)."""
    w = World(clock)
    user = await w.user()
    oldest = clock.now() - DAY
    w.seed_scores(user, [oldest] + [clock.now() - timedelta(hours=h) for h in range(1, 10)])

    outcome = await w.ask(user, pair_of())

    assert outcome.created is True


async def test_a_user_one_second_before_the_oldest_score_expires_is_refused(
    clock: FixedClock,
) -> None:
    w = World(clock)
    user = await w.user()
    oldest = clock.now() - DAY + timedelta(seconds=1)
    w.seed_scores(user, [oldest] + [clock.now() - timedelta(hours=h) for h in range(1, 10)])

    with pytest.raises(TooManyFitScores) as info:
        await w.ask(user, pair_of())

    assert info.value.retry_not_before == clock.now() + timedelta(seconds=1)


async def test_a_user_above_the_limit_waits_for_the_count_to_drop_below_it(
    clock: FixedClock,
) -> None:
    """12 scores (a claim may leave a user above 10, AC-35): they may score again once only 9 are in
    the window, which is when `scored[12 - 10]`, the third oldest, passes 24 h (AC-12 as amended)."""
    w = World(clock)
    user = await w.user()
    instants = [clock.now() - timedelta(hours=h) for h in range(12, 0, -1)]  # oldest first
    w.seed_scores(user, list(reversed(instants)))  # inserted newest first, on purpose

    with pytest.raises(TooManyFitScores) as info:
        await w.ask(user, pair_of())

    assert info.value.count == 12
    assert info.value.limit == MAX_PER_USER
    assert info.value.retry_not_before == instants[2] + DAY


async def test_a_user_whose_scores_are_all_older_than_24_hours_is_scored(
    clock: FixedClock,
) -> None:
    w = World(clock)
    user = await w.user()
    w.seed_scores(user, [clock.now() - timedelta(hours=25 + n) for n in range(MAX_PER_USER)])

    outcome = await w.ask(user, pair_of())

    assert outcome.created is True


# --- AC-10: a failure consumes no limit --------------------------------------------------------------------


async def test_a_failed_attempt_stores_nothing_so_the_next_one_is_not_counted_against(
    clock: FixedClock,
) -> None:
    failure = FitScoringFailed(FitScoreFailureReason.LLM_ERROR, usage(0, 0, unmetered=1))
    w = World(clock, outcome=failure)
    guest = await w.guest()
    w.seed_scores(guest, [clock.now() - timedelta(hours=h) for h in (1, 2)])

    with pytest.raises(FitScoringFailed):
        await w.ask(guest, pair_of())

    assert len(w.scores.all()) == 2
    # The seeded count is untouched, and the real spend was recorded: it consumed money, not a slot.
    assert w.ledger.spent == {w.day: WORST_ATTEMPT}


# --- AC-13: the reservation's arithmetic -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cv_text", "posting_text", "overhead", "max_output", "attempts", "expected"),
    [
        # 4000 in -> 400 exact; 512 out -> 204.8 rounds up to 205; 605 an attempt.
        ("c" * 1000, "p" * 500, 2500, 512, 2, 1210),
        # Whitespace is collapsed before counting: 499 + 1 + 500 characters, the same 1000.
        ("c" * 499 + "  \n\t  " + "c" * 500, "p" * 500, 2500, 512, 2, 1210),
        # 2 in -> 0.2 rounds up to 1; 1 out -> 0.4 rounds up to 1. Each direction rounds on its own.
        ("c", "p", 0, 1, 1, 2),
        ("c", "p", 0, 1, 3, 6),
        # 10,000 in -> 1000 exact; 500 out -> 200 exact.
        ("c" * 6000, "p" * 3000, 1000, 500, 1, 1200),
        ("c" * 6000, "p" * 3000, 1000, 500, 3, 3600),
    ],
)
async def test_the_reservation_is_the_worst_attempt_times_the_attempts_allowed(
    clock: FixedClock,
    cv_text: str,
    posting_text: str,
    overhead: int,
    max_output: int,
    attempts: int,
    expected: int,
) -> None:
    w = World(clock)

    await w.ask(
        await w.guest(),
        pair_of(cv_text, posting_text),
        overhead=overhead,
        max_output_tokens=max_output,
        max_attempts=attempts,
    )

    assert [(r[1], r[2].value) for r in w.ledger.reservations] == [(MicroUsd(expected), 2_000_000)]
    assert w.ledger.reservations[0][0] == w.day


# --- AC-14: the settlement's arithmetic ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "unmetered", "expected"),
    [
        (1700, 150, 0, 230),  # 170 + 60
        (1, 1, 0, 2),  # each direction rounds up to 1
        (5, 5, 0, 3),  # 0.5 -> 1, and 2.0 exact
        (0, 0, 0, 0),
        (1700, 150, 1, 835),  # 230 plus one unmetered attempt at its worst case, 605
        (0, 0, 2, 1210),
    ],
)
async def test_a_success_settles_to_its_actual_cost_in_the_same_call_as_the_row(
    clock: FixedClock, input_tokens: int, output_tokens: int, unmetered: int, expected: int
) -> None:
    w = World(clock, outcome=assessed(used=usage(input_tokens, output_tokens, unmetered)))

    outcome = await w.ask(await w.guest(), pair_of())

    assert outcome.stored.cost == MicroUsd(expected)
    assert w.ledger.settlements == [(w.day, MicroUsd(RESERVED), MicroUsd(expected))]
    assert w.ledger.spent == {w.day: expected}


# --- S-1 to S-9: a failed call settles to what it consumed, stores nothing --------------------------------------


_TIMED_OUT = FitScoringFailed(FitScoreFailureReason.LLM_TIMED_OUT, usage(0, 0, unmetered=2))
_RATE_LIMITED = FitScoringFailed(FitScoreFailureReason.LLM_RATE_LIMITED, usage(0, 0, unmetered=2))
_REFUSED = FitScoringFailed(FitScoreFailureReason.LLM_REFUSED, usage(1700, 150, 0))
_NOT_JSON = FitScoringOutputInvalid("not_json", usage(3400, 300, 0))
_WRONG = FitScoringOutputInvalid("gap_not_in_posting", usage(1700, 150, 0))
_OTHER = FitScoringFailed(FitScoreFailureReason.LLM_ERROR, usage(0, 0, unmetered=1))
_NOT_CONFIGURED = FitScoringFailed(FitScoreFailureReason.LLM_UNAVAILABLE, usage(0, 0, 0))
_NETWORK = FitScoringFailed(FitScoreFailureReason.LLM_UNAVAILABLE, usage(0, 0, unmetered=2))
_TOO_LARGE = FitScoringFailed(FitScoreFailureReason.INPUTS_TOO_LARGE, usage(0, 0, 0))


@pytest.mark.parametrize(
    ("failure", "settled"),
    [
        pytest.param(_TIMED_OUT, 1210, id="S-1 timeout: two unmetered attempts at worst case"),
        pytest.param(_RATE_LIMITED, 1210, id="S-2 rate limited"),
        pytest.param(_REFUSED, 230, id="S-3 refusal: metered, never retried"),
        pytest.param(_NOT_JSON, 460, id="S-4 unparseable: two metered attempts"),
        pytest.param(_WRONG, 230, id="S-5 parses but wrong"),
        pytest.param(_OTHER, 605, id="S-6 any other provider error"),
        pytest.param(_NOT_CONFIGURED, 0, id="S-7 not configured: settles to nothing"),
        pytest.param(_NETWORK, 1210, id="S-8 network or 5xx"),
        pytest.param(_TOO_LARGE, 0, id="S-9 inputs too large: no call, settles to nothing"),
    ],
)
async def test_a_failed_call_settles_to_what_it_consumed_and_leaves_no_row(
    clock: FixedClock, failure: FitScoringFailed, settled: int
) -> None:
    w = World(clock, outcome=failure)

    with pytest.raises(FitScoringFailed) as info:
        await w.ask(await w.guest(), pair_of())

    assert info.value is failure
    assert w.ledger.settlements == [(w.day, MicroUsd(RESERVED), MicroUsd(settled))]
    assert w.ledger.spent == {w.day: settled}
    assert w.journal == ["scored_at_since", "reserve", "assess", "settle"]
    assert w.scores.all() == []
    assert w.scores.store_calls == 0
    assert w.events.published == []


# --- S-10, S-14: the cap, and the reservation failing ---------------------------------------------------------------


async def test_a_reservation_the_day_cannot_take_is_busy_with_no_call_settlement_or_row(
    clock: FixedClock,
) -> None:
    w = World(clock)
    guest = await w.guest()
    w.ledger.spent[w.day] = 1_999_000  # 1,000 left; this one needs 1,210

    with pytest.raises(FitScoreSpendCapReached):
        await w.ask(guest, pair_of())

    assert w.journal == ["scored_at_since", "reserve"]
    assert w.scorer.calls == []
    assert w.ledger.settlements == []
    assert w.ledger.spent == {w.day: 1_999_000}
    assert w.scores.all() == []
    assert w.events.published == []


async def test_a_reservation_that_exactly_fits_what_is_left_is_taken(clock: FixedClock) -> None:
    w = World(clock)

    outcome = await w.ask(await w.guest(), pair_of(), daily_cap=MicroUsd(RESERVED))

    assert outcome.created is True


async def test_a_reservation_one_micro_dollar_over_the_cap_is_busy(clock: FixedClock) -> None:
    w = World(clock)

    with pytest.raises(FitScoreSpendCapReached):
        await w.ask(await w.guest(), pair_of(), daily_cap=MicroUsd(RESERVED - 1))

    assert w.scorer.calls == []


async def test_a_cap_of_zero_is_always_busy(clock: FixedClock) -> None:
    w = World(clock)

    with pytest.raises(FitScoreSpendCapReached):
        await w.ask(await w.guest(), pair_of(), daily_cap=MicroUsd(0))

    assert w.scorer.calls == []


async def test_a_reservation_that_fails_to_commit_means_no_call_and_no_row(
    clock: FixedClock,
) -> None:
    down = LedgerDown()
    w = World(clock, reserve_raises=down)

    with pytest.raises(LedgerDown) as info:
        await w.ask(await w.guest(), pair_of())

    assert info.value is down
    assert w.scorer.calls == []
    assert w.ledger.settlements == []
    assert w.scores.all() == []
    assert w.events.published == []


# --- S-15, S-16: when settling fails --------------------------------------------------------------------------------


async def test_when_settling_a_failure_fails_the_original_error_wins_from_the_settles_exception(
    clock: FixedClock,
) -> None:
    down = LedgerDown()
    failure = FitScoringFailed(FitScoreFailureReason.LLM_TIMED_OUT, usage(0, 0, unmetered=2))
    w = World(clock, outcome=failure, settle_raises=down)

    with pytest.raises(FitScoringFailed) as info:
        await w.ask(await w.guest(), pair_of())

    assert info.value is failure
    assert info.value.__cause__ is down
    assert w.ledger.spent == {w.day: RESERVED}  # the reservation stands


async def test_when_settling_after_a_refused_store_fails_the_refusal_still_wins(
    clock: FixedClock,
) -> None:
    down = LedgerDown()
    gone = BaseCvNotFound("deleted during the call")
    w = World(clock, store_raises=gone, settle_raises=down)

    with pytest.raises(BaseCvNotFound) as info:
        await w.ask(await w.guest(), pair_of())

    assert info.value is gone
    assert info.value.__cause__ is down


async def test_a_settlement_that_fails_after_a_success_propagates_and_publishes_nothing(
    clock: FixedClock,
) -> None:
    """No earlier error to defer to (S-15): the settle is what commits the row, so its failure is the
    answer, and nothing is announced for a score that did not commit."""
    down = LedgerDown()
    w = World(clock, settle_raises=down)

    with pytest.raises(LedgerDown) as info:
        await w.ask(await w.guest(), pair_of())

    assert info.value is down
    assert w.events.published == []
    assert "publish" not in w.journal


# --- S-25 to S-27: paid for, nothing stored ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(BaseCvNotFound("cv gone"), id="S-25 the CV was deleted"),
        pytest.param(JobPostingNotFound("posting gone"), id="S-25 the posting was deleted"),
        pytest.param(GuestSessionExpired("session gone"), id="S-26 claimed or purged"),
        pytest.param(UserNotFound("user gone"), id="S-27 account erased"),
    ],
)
async def test_a_store_refused_because_what_it_points_at_went_settles_and_leaves_no_row(
    clock: FixedClock, refusal: Exception
) -> None:
    w = World(clock, store_raises=refusal)

    with pytest.raises(type(refusal)) as info:
        await w.ask(await w.guest(), pair_of())

    assert info.value is refusal
    assert w.ledger.settlements == [(w.day, MicroUsd(RESERVED), MicroUsd(USUAL_COST))]
    assert w.ledger.spent == {w.day: USUAL_COST}
    assert w.journal == ["scored_at_since", "reserve", "assess", "store", "settle"]
    assert w.scores.all() == []
    assert w.events.published == []


async def test_a_database_failure_in_store_is_not_settled_so_the_reservation_stands(
    clock: FixedClock,
) -> None:
    """S-15: the call finished, the store failed with something that is not a domain refusal. The
    transaction is gone, so a settle would be written into nothing: the reservation stands."""
    down = StoreDown()
    w = World(clock, store_raises=down)

    with pytest.raises(StoreDown):
        await w.ask(await w.guest(), pair_of())

    assert w.ledger.settlements == []
    assert w.ledger.spent == {w.day: RESERVED}
    assert w.events.published == []


# --- AC-21's unit half: cancellation still cancels --------------------------------------------------------------------


async def test_cancelling_during_the_call_raises_cancelled_error_and_settles_nothing(
    clock: FixedClock,
) -> None:
    w = World(clock, block=True)
    guest = await w.guest()
    task = asyncio.ensure_future(w.ask(guest, pair_of()))
    started = asyncio.ensure_future(w.scorer.started.wait())
    await asyncio.wait({task, started}, return_when=asyncio.FIRST_COMPLETED, timeout=5)
    if task.done():
        await task  # surfaces whatever ended it early
    assert w.scorer.calls, "assess was never reached"

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert w.ledger.settlements == []
    assert w.ledger.spent == {w.day: RESERVED}  # AC-17: the reservation stands as spend
    assert w.scores.all() == []
    assert w.scores.store_calls == 0
    assert w.events.published == []
    assert w.journal == ["scored_at_since", "reserve", "assess"]
    started.cancel()


# --- The loser of a two-tab race ---------------------------------------------------------------------------------------


async def test_the_loser_of_a_store_returns_the_winner_publishes_nothing_and_still_settles(
    clock: FixedClock,
) -> None:
    guest = a_guest_owner()
    pair = pair_of()
    winner = a_stored_score(guest, clock.now() - timedelta(seconds=5), pair=pair, gaps=("Rust",))

    def rival_lands() -> None:
        w.scores.seed(winner)  # committed by the other tab while this request is inside the call

    w = World(clock, on_assess=rival_lands)

    outcome = await w.ask(guest, pair)

    assert outcome.created is False
    assert outcome.stored is winner
    assert w.events.published == []
    assert w.ledger.settlements == [(w.day, MicroUsd(RESERVED), MicroUsd(USUAL_COST))]
    assert w.scores.all() == [winner]
