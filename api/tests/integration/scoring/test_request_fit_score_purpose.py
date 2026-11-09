"""Application tests for `RequestFitScore`'s slice-5.2 changes (T15 RED, AC-19, AC-27, S-4, S-10).

`purpose`: a `CHECK` is limited per owner and counts `fit_scores` when it **creates** a row; a
`COMPARISON` (the paid-for before/after) skips the owner limit and the counter, but still reserves
against the day's cap. A refused reservation counts `fit_scores_busy` for either purpose, and a call
that is refused before any reservation (the owner limit, `INPUTS_TOO_LARGE`) counts nothing.
`draft_of` is stored on the row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tailorcraft.domain.scoring.errors import (
    FitScoreSpendCapReached,
    FitScoringFailed,
    TooManyFitScores,
)
from tailorcraft.domain.scoring.value_objects import ScoredRunRef, ScoringPurpose
from tailorcraft.domain.shared.counters import DemandCounter
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.scoring.support import (
    MAX_CV_CHARACTERS,
    MAX_PER_SESSION,
    MAX_PER_USER,
    World,
    a_guest_owner,
    a_stored_score,
    assessed,
    pair_of,
)


async def test_a_created_check_counts_one_fit_score_on_the_instants_utc_day(
    clock: FixedClock,
) -> None:
    w = World(clock)

    await w.ask(await w.guest(), pair_of())

    assert w.counters.calls == [(DemandCounter.FIT_SCORES, clock.now().date())]


async def test_the_count_day_is_the_instants_not_the_systems(clock: FixedClock) -> None:
    late = FixedClock(datetime(2026, 9, 4, 23, 59, 59, tzinfo=UTC))
    w = World(late)

    await w.ask(await w.guest(), pair_of())

    assert w.counters.calls == [(DemandCounter.FIT_SCORES, late.now().date())]


async def test_the_count_lands_before_the_settlement_that_commits_the_row(
    clock: FixedClock,
) -> None:
    """Transaction B commits at `settle`, so the count must be written before it to share it."""
    w = World(clock, counters_in_journal=True)

    await w.ask(await w.guest(), pair_of())

    assert w.journal == [
        "scored_at_since",
        "reserve",
        "assess",
        "store",
        "count:fit_scores",
        "settle",
        "publish",
    ]


async def test_the_loser_of_a_two_tab_race_counts_nothing(clock: FixedClock) -> None:
    guest = a_guest_owner()
    pair = pair_of()
    winner = a_stored_score(guest, clock.now() - timedelta(seconds=5), pair=pair)
    w = World(clock, on_assess=lambda: w.scores.seed(winner))

    outcome = await w.ask(guest, pair)

    assert outcome.created is False
    assert w.counters.calls == []


async def test_a_failed_assessment_counts_nothing(clock: FixedClock) -> None:
    from tailorcraft.domain.scoring.value_objects import FitScoreFailureReason

    w = World(
        clock, outcome=FitScoringFailed(FitScoreFailureReason.LLM_TIMED_OUT, assessed().usage)
    )

    with pytest.raises(FitScoringFailed):
        await w.ask(await w.guest(), pair_of())

    assert w.counters.calls == []


async def test_a_check_refused_by_the_owner_limit_counts_nothing(clock: FixedClock) -> None:
    w = World(clock)
    guest = await w.guest()
    w.seed_scores(guest, [clock.now() - timedelta(hours=h) for h in range(1, MAX_PER_SESSION + 1)])

    with pytest.raises(TooManyFitScores):
        await w.ask(guest, pair_of())

    assert w.counters.calls == []


async def test_a_refused_reservation_counts_busy_once_and_still_raises(clock: FixedClock) -> None:
    w = World(clock)
    guest = await w.guest()
    w.ledger.spent[w.day] = 1_999_000

    with pytest.raises(FitScoreSpendCapReached):
        await w.ask(guest, pair_of())

    assert w.counters.calls == [(DemandCounter.FIT_SCORES_BUSY, clock.now().date())]
    assert w.scores.all() == []


async def test_a_too_large_cv_is_refused_before_any_reservation_so_it_is_not_busy(
    clock: FixedClock,
) -> None:
    """S-10."""
    w = World(clock)

    with pytest.raises(FitScoringFailed):
        await w.ask(await w.guest(), pair_of(cv_text="c" * (MAX_CV_CHARACTERS + 1)))

    assert w.counters.calls == []


# --- COMPARISON -----------------------------------------------------------------------------------


async def test_a_comparison_is_not_held_to_the_users_limit(clock: FixedClock) -> None:
    """AC-19: the credit paid for it."""
    w = World(clock)
    user = await w.user()
    w.seed_scores(user, [clock.now() - timedelta(hours=1)] * MAX_PER_USER)

    outcome = await w.ask(user, pair_of(), purpose=ScoringPurpose.COMPARISON)

    assert outcome.created is True
    assert "scored_at_since" not in w.journal


async def test_a_check_beside_it_is_still_held_to_the_limit(clock: FixedClock) -> None:
    """The exemption is the purpose, not the owner."""
    w = World(clock)
    user = await w.user()
    w.seed_scores(user, [clock.now() - timedelta(hours=1)] * MAX_PER_USER)

    with pytest.raises(TooManyFitScores):
        await w.ask(user, pair_of(), purpose=ScoringPurpose.CHECK)


async def test_a_created_comparison_counts_no_fit_score(clock: FixedClock) -> None:
    w = World(clock)

    await w.ask(await w.user(), pair_of(), purpose=ScoringPurpose.COMPARISON)

    assert w.counters.calls == []


async def test_a_comparison_reserves_and_settles_like_a_check(clock: FixedClock) -> None:
    w = World(clock)

    await w.ask(await w.user(), pair_of(), purpose=ScoringPurpose.COMPARISON)

    assert w.journal == ["reserve", "assess", "store", "settle", "publish"]


async def test_a_comparison_at_the_daily_cap_is_busy_and_counted(clock: FixedClock) -> None:
    """S-4: the cap binds a comparison too."""
    w = World(clock)
    w.ledger.spent[w.day] = 1_999_000

    with pytest.raises(FitScoreSpendCapReached):
        await w.ask(await w.user(), pair_of(), purpose=ScoringPurpose.COMPARISON)

    assert w.counters.calls == [(DemandCounter.FIT_SCORES_BUSY, clock.now().date())]
    assert w.scorer.calls == []


async def test_a_comparison_too_large_for_the_scorer_is_refused_before_any_reservation(
    clock: FixedClock,
) -> None:
    w = World(clock)

    with pytest.raises(FitScoringFailed):
        await w.ask(
            await w.user(),
            pair_of(cv_text="c" * (MAX_CV_CHARACTERS + 1)),
            purpose=ScoringPurpose.COMPARISON,
        )

    assert w.ledger.reservations == []
    assert w.counters.calls == []


# --- draft_of --------------------------------------------------------------------------------------


async def test_draft_of_is_stored_on_the_row(clock: FixedClock) -> None:
    w = World(clock)
    ref = ScoredRunRef(uuid4())

    outcome = await w.ask(
        await w.user(), pair_of(), purpose=ScoringPurpose.COMPARISON, draft_of=ref
    )

    assert outcome.stored.draft_of == ref


async def test_a_check_stores_no_draft_of(clock: FixedClock) -> None:
    w = World(clock)

    outcome = await w.ask(await w.guest(), pair_of())

    assert outcome.stored.draft_of is None
