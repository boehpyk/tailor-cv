"""Application tests for `ScoreTailoringRun` (slice 5.2, T15 RED, AC-18, AC-19, AC-21, AC-22's call
half, S-4, S-5, S-6, S-7, S-8, S-10, plan §0.8).

One side of a run's comparison. **The order is the error order**: the run (404, not-mine included),
then `SUCCEEDED` (S-5), then the base CV (S-6), then the pair. `BEFORE` is the ordinary pair of the
run's base CV and posting; `AFTER` scores the run's **draft** CV, converted to plain text by the
`DraftTextPort`, never the revision (ADR-0015), against the same posting, and keeps the base CV's id
so deleting the CV takes the score. A stored row for the pair is returned without a call (200).
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.scoring.resolve_scoring_pair import ScoringPair
from tailorcraft.application.scoring.score_tailoring_run import ScoreTailoringRunCommand
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.scoring.errors import (
    ComparisonUnavailable,
    FitScoreSpendCapReached,
    FitScoringFailed,
    TooManyFitScores,
)
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.value_objects import (
    ComparisonSide,
    ComparisonUnavailableReason,
    CvForScoring,
    FitScoreFailureReason,
    MicroUsd,
    ScoredRunRef,
)
from tailorcraft.domain.shared.counters import DemandCounter
from tailorcraft.domain.tailoring.errors import TailoringRunNotFound, TailoringRunNotSucceeded
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import FakeDraftText
from tests.integration.owners import queued_run, running_run
from tests.integration.scoring.comparison_support import (
    BEFORE_TEXT,
    DRAFT_MARKDOWN,
    REVISION_MARKDOWN,
    ComparisonWorld,
    reserved_for,
)
from tests.integration.scoring.support import (
    MAX_CV_CHARACTERS,
    MAX_PER_USER,
    USUAL_COST,
    assessed,
)


def _cmd(owner: UserOwner, run: TailoringRun, side: ComparisonSide) -> ScoreTailoringRunCommand:
    return ScoreTailoringRunCommand(owner=owner, tailoring_run_id=run.id, side=side)


# --- BEFORE ---------------------------------------------------------------------------------------


async def test_before_scores_the_runs_base_cv_and_posting_as_an_ordinary_pair(
    clock: FixedClock,
) -> None:
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    pair = await w.before_pair(run)

    outcome = await w.score_run()(_cmd(w.owner, run, ComparisonSide.BEFORE))

    assert outcome.created is True
    assert w.scorer.calls == [(pair.cv, pair.posting)]
    stored = outcome.stored
    assert stored.base_cv_id == pair.cv_id
    assert stored.cv_fingerprint == pair.cv_fingerprint
    assert stored.draft_of is None
    assert w.scores.all() == [stored]


async def test_before_returns_a_score_already_on_file_for_the_pair_without_a_call(
    clock: FixedClock,
) -> None:
    """AC-18: a user who checked that pair already gets the stored row (200)."""
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    seeded = w.seed_before(await w.before_pair(run), 55)

    outcome = await w.score_run()(_cmd(w.owner, run, ComparisonSide.BEFORE))

    assert outcome.created is False
    assert outcome.stored is seeded
    assert w.scorer.calls == []
    assert w.ledger.reservations == []


# --- AFTER ----------------------------------------------------------------------------------------


async def test_after_scores_the_drafts_plain_text_through_the_port_with_the_same_posting(
    clock: FixedClock,
) -> None:
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    before = await w.before_pair(run)
    plain = await FakeDraftText().to_plain_text(DRAFT_MARKDOWN)

    outcome = await w.score_run()(_cmd(w.owner, run, ComparisonSide.AFTER))

    assert w.draft_text.calls == [DRAFT_MARKDOWN]
    assert w.scorer.calls == [(CvForScoring(plain), before.posting)]
    stored = outcome.stored
    assert stored.draft_of == ScoredRunRef(run.id.value)
    assert stored.base_cv_id == before.cv_id  # so deleting the CV takes the draft score too
    assert stored.job_posting_id == before.posting_id
    assert stored.posting_fingerprint == before.posting_fingerprint
    assert stored.cv_fingerprint != before.cv_fingerprint  # the draft is not the base CV


async def test_after_scores_the_draft_never_the_users_revision(clock: FixedClock) -> None:
    """ADR-0015: the draft is never overwritten, and OQ-5 scores what tailoring produced."""
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    await w.revise(run)
    assert run.current_documents is not None
    assert run.current_documents.cv.value == REVISION_MARKDOWN  # the revision really is current

    await w.score_run()(_cmd(w.owner, run, ComparisonSide.AFTER))

    assert w.draft_text.calls == [DRAFT_MARKDOWN]


async def test_after_returns_a_stored_draft_score_without_a_call(clock: FixedClock) -> None:
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    seeded = w.seed_after(run, await w.after_pair(run), 70)

    outcome = await w.score_run()(_cmd(w.owner, run, ComparisonSide.AFTER))

    assert outcome.created is False
    assert outcome.stored is seeded
    assert w.scorer.calls == []
    assert w.ledger.reservations == []


async def test_the_two_sides_are_two_rows_that_never_collide(clock: FixedClock) -> None:
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    score = w.score_run()

    before = await score(_cmd(w.owner, run, ComparisonSide.BEFORE))
    after = await score(_cmd(w.owner, run, ComparisonSide.AFTER))

    assert before.created is True
    assert after.created is True
    assert before.stored.id != after.stored.id
    assert len(w.scorer.calls) == 2


# --- limits, cap, counters (AC-19) ---------------------------------------------------------------------


@pytest.mark.parametrize("side", list(ComparisonSide))
async def test_neither_side_consults_the_owner_limit_nor_counts_a_fit_score(
    side: ComparisonSide, clock: FixedClock
) -> None:
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    w.seed_scores(w.owner, [clock.now() - timedelta(hours=1)] * MAX_PER_USER)

    try:
        outcome = await w.score_run()(_cmd(w.owner, run, side))
    except TooManyFitScores:
        pytest.fail("a comparison was held to the per-user fit-score limit")

    assert outcome.created is True
    assert w.counters.calls == []


@pytest.mark.parametrize("side", list(ComparisonSide))
async def test_at_the_daily_cap_either_side_is_busy_and_counts_busy(
    side: ComparisonSide, clock: FixedClock
) -> None:
    """S-4."""
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    w.ledger.spent[w.day] = 1_999_000

    with pytest.raises(FitScoreSpendCapReached):
        await w.score_run()(_cmd(w.owner, run, side))

    assert w.counters.calls == [(DemandCounter.FIT_SCORES_BUSY, clock.now().date())]
    assert w.scorer.calls == []


async def test_a_side_scored_costs_what_a_check_costs_and_settles(clock: FixedClock) -> None:
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None

    await w.score_run()(_cmd(w.owner, run, ComparisonSide.BEFORE))

    assert w.ledger.settlements == [
        (w.day, MicroUsd(reserved_for(BEFORE_TEXT)), MicroUsd(USUAL_COST))
    ]


async def test_a_failed_assessment_is_the_scorers_failure_for_that_side(
    clock: FixedClock,
) -> None:
    """S-1, S-2, S-3: the scorer's failure (a timeout here) propagates as 5.1's `FitScoringFailed`."""
    failure = FitScoringFailed(FitScoreFailureReason.LLM_TIMED_OUT, assessed().usage)
    w = ComparisonWorld(clock, outcome=failure)
    run = await w.succeeded_run()
    assert w.owner is not None

    with pytest.raises(FitScoringFailed) as info:
        await w.score_run()(_cmd(w.owner, run, ComparisonSide.AFTER))

    assert info.value is failure
    assert w.scores.all() == []


async def test_a_draft_too_long_for_the_scorer_is_refused_before_any_reservation(
    clock: FixedClock,
) -> None:
    """S-10."""
    w = ComparisonWorld(clock)
    w.draft_text = _HugeDraft()
    run = await w.succeeded_run()
    assert w.owner is not None

    with pytest.raises(FitScoringFailed) as info:
        await w.score_run()(_cmd(w.owner, run, ComparisonSide.AFTER))

    assert info.value.reason is FitScoreFailureReason.INPUTS_TOO_LARGE
    assert w.ledger.reservations == []
    assert w.counters.calls == []


class _HugeDraft(FakeDraftText):
    async def to_plain_text(self, markdown: str) -> str:
        return "d" * (MAX_CV_CHARACTERS + 1)


# --- refusals and their order -----------------------------------------------------------------------


@pytest.mark.parametrize("side", list(ComparisonSide))
async def test_an_unknown_run_is_not_found(side: ComparisonSide, clock: FixedClock) -> None:
    w = ComparisonWorld(clock)
    await w.succeeded_run()
    assert w.owner is not None

    with pytest.raises(TailoringRunNotFound):
        await w.score_run()(
            ScoreTailoringRunCommand(
                owner=w.owner, tailoring_run_id=TailoringRunId(value=uuid4()), side=side
            )
        )

    assert w.scorer.calls == []


async def test_someone_elses_run_is_not_found_exactly_like_an_unknown_one(
    clock: FixedClock,
) -> None:
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    stranger = await w.user("stranger@example.com")

    with pytest.raises(TailoringRunNotFound) as info:
        await w.score_run()(_cmd(stranger, run, ComparisonSide.BEFORE))

    assert type(info.value) is TailoringRunNotFound
    assert w.scorer.calls == []
    assert w.journal == []


@pytest.mark.parametrize("side", list(ComparisonSide))
@pytest.mark.parametrize("make", [queued_run, running_run], ids=["queued", "running"])
async def test_a_run_not_yet_succeeded_is_refused_with_nothing_scored(
    side: ComparisonSide, make: object, clock: FixedClock
) -> None:
    """S-5."""
    w = ComparisonWorld(clock)
    base = await w.succeeded_run()
    assert w.owner is not None
    run = make(  # type: ignore[operator]
        w.owner, w.earlier, base_cv_id=base.base_cv_id, job_posting_id=base.job_posting_id
    )
    await w.runs.add(run)

    with pytest.raises(TailoringRunNotSucceeded):
        await w.score_run()(_cmd(w.owner, run, side))

    assert (w.scorer.calls, w.ledger.reservations, w.journal) == ([], [], [])


async def test_a_failed_run_is_refused_as_not_succeeded(clock: FixedClock) -> None:
    w = ComparisonWorld(clock)
    run = await w.failed_run()
    assert w.owner is not None

    with pytest.raises(TailoringRunNotSucceeded):
        await w.score_run()(_cmd(w.owner, run, ComparisonSide.BEFORE))


@pytest.mark.parametrize("side", list(ComparisonSide))
async def test_a_run_whose_base_cv_was_deleted_is_unavailable(
    side: ComparisonSide, clock: FixedClock
) -> None:
    """S-6."""
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    assert w.cv_id is not None
    await w.cvs.remove(w.cv_id, w.owner)

    with pytest.raises(ComparisonUnavailable) as info:
        await w.score_run()(_cmd(w.owner, run, side))

    assert info.value.reason is ComparisonUnavailableReason.BASE_CV_DELETED
    assert (w.scorer.calls, w.ledger.reservations) == ([], [])


async def test_not_succeeded_is_reported_before_a_deleted_base_cv(clock: FixedClock) -> None:
    """The order is the error order (plan §4)."""
    w = ComparisonWorld(clock)
    run = await w.failed_run()
    assert w.owner is not None
    assert w.cv_id is not None
    await w.cvs.remove(w.cv_id, w.owner)

    with pytest.raises(TailoringRunNotSucceeded):
        await w.score_run()(_cmd(w.owner, run, ComparisonSide.BEFORE))


async def test_a_store_refused_because_the_run_went_during_the_call_settles_and_leaves_no_row(
    clock: FixedClock,
) -> None:
    """S-7: `store` raises `TailoringRunNotFound` when the run was deleted meanwhile."""
    gone = TailoringRunNotFound("deleted during the call")
    w = ComparisonWorld(clock, store_raises=gone)
    run = await w.succeeded_run()
    assert w.owner is not None

    with pytest.raises(TailoringRunNotFound) as info:
        await w.score_run()(_cmd(w.owner, run, ComparisonSide.AFTER))

    assert info.value is gone
    assert w.scores.all() == []
    after_text = await FakeDraftText().to_plain_text(DRAFT_MARKDOWN)
    assert w.ledger.settlements == [
        (w.day, MicroUsd(reserved_for(after_text)), MicroUsd(USUAL_COST))
    ]


async def test_two_tabs_comparing_at_once_share_one_row(clock: FixedClock) -> None:
    """S-8: the loser of 5.1's ON CONFLICT returns the winner (200) and publishes nothing."""
    winner: list[FitScore] = []
    pairs: list[ScoringPair] = []
    world: list[ComparisonWorld] = []
    w = ComparisonWorld(clock, on_assess=lambda: winner.append(world[0].seed_before(pairs[0], 61)))
    world.append(w)
    run = await w.succeeded_run()
    assert w.owner is not None
    pairs.append(await w.before_pair(run))

    outcome = await w.score_run()(_cmd(w.owner, run, ComparisonSide.BEFORE))

    assert outcome.created is False
    assert outcome.stored is winner[0]
    assert w.scores.all() == [winner[0]]
    assert w.events.published == []
