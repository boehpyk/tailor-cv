"""Application tests for `GetTailoringRunFitScores` (slice 5.2, T15 RED, AC-20, AC-21, AC-23, S-6, S-9).

The stored before and after of a run and the verdict **computed by the domain**. It reads: it never
calls the model, never reserves, never counts. `comparable` is `None` unless both sides exist, and
`comparison` is set only when they are comparable (the same model and prompt version, OQ-4);
`unavailable` says why no comparison can exist, and then both sides are `None`.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tailorcraft.application.scoring.get_tailoring_run_fit_scores import (
    GetTailoringRunFitScoresQuery,
    TailoringRunFitScores,
)
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.scoring.comparison import ScoreComparison
from tailorcraft.domain.scoring.value_objects import (
    ComparisonUnavailableReason,
    ComparisonVerdict,
)
from tailorcraft.domain.tailoring.errors import TailoringRunNotFound
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.scoring.comparison_support import ComparisonWorld


async def _get(w: ComparisonWorld, run: TailoringRun, owner: UserOwner) -> TailoringRunFitScores:
    return await w.fit_scores()(GetTailoringRunFitScoresQuery(owner=owner, tailoring_run_id=run.id))


def _nothing_was_spent_or_asked(w: ComparisonWorld) -> None:
    assert w.scorer.calls == []
    assert w.ledger.reservations == []
    assert w.counters.calls == []
    assert w.journal == []


@pytest.mark.parametrize(
    ("before", "after", "delta", "verdict"),
    [
        (60, 70, 10, ComparisonVerdict.HIGHER),
        (60, 63, 3, ComparisonVerdict.ABOUT_THE_SAME),
        (60, 40, -20, ComparisonVerdict.LOWER),
    ],
)
async def test_both_sides_stored_gives_the_domains_delta_and_verdict(
    before: int, after: int, delta: int, verdict: ComparisonVerdict, clock: FixedClock
) -> None:
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    stored_before = w.seed_before(await w.before_pair(run), before)
    stored_after = w.seed_after(run, await w.after_pair(run), after)

    result = await _get(w, run, w.owner)

    assert result == TailoringRunFitScores(
        before=stored_before,
        after=stored_after,
        comparison=ScoreComparison(delta=delta, verdict=verdict),
        comparable=True,
        unavailable=None,
    )
    _nothing_was_spent_or_asked(w)


async def test_nothing_stored_is_two_nulls_and_no_call(clock: FixedClock) -> None:
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None

    result = await _get(w, run, w.owner)

    assert result == TailoringRunFitScores(
        before=None, after=None, comparison=None, comparable=None, unavailable=None
    )
    _nothing_was_spent_or_asked(w)


async def test_only_the_before_stored_has_no_comparison(clock: FixedClock) -> None:
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    stored = w.seed_before(await w.before_pair(run), 50)

    result = await _get(w, run, w.owner)

    assert (result.before, result.after) == (stored, None)
    assert (result.comparison, result.comparable) == (None, None)


async def test_only_the_after_stored_has_no_comparison(clock: FixedClock) -> None:
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    stored = w.seed_after(run, await w.after_pair(run), 50)

    result = await _get(w, run, w.owner)

    assert (result.before, result.after) == (None, stored)
    assert (result.comparison, result.comparable) == (None, None)


async def test_scores_from_two_scorer_versions_are_returned_but_not_compared(
    clock: FixedClock,
) -> None:
    """AC-23, S-9: an old "before" is shown beside a new "after", flagged, never re-scored."""
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    old = w.seed_before(await w.before_pair(run), 50, model="gemini-old")
    new = w.seed_after(run, await w.after_pair(run), 70)

    result = await _get(w, run, w.owner)

    assert (result.before, result.after) == (old, new)
    assert (result.comparable, result.comparison) == (False, None)
    _nothing_was_spent_or_asked(w)


async def test_the_after_found_is_the_drafts_even_when_the_user_has_revised_the_cv(
    clock: FixedClock,
) -> None:
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    stored = w.seed_after(run, await w.after_pair(run), 70)
    await w.revise(run)

    result = await _get(w, run, w.owner)

    assert result.after is stored


async def test_another_owners_scores_for_the_same_text_are_not_this_runs(
    clock: FixedClock,
) -> None:
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    stranger = await w.user("stranger@example.com")
    pair = await w.before_pair(run)
    w.owner, mine = stranger, w.owner
    w.seed_before(pair, 40)
    w.owner = mine

    result = await _get(w, run, mine)

    assert result.before is None


async def test_a_run_not_succeeded_is_unavailable_not_an_error(clock: FixedClock) -> None:
    w = ComparisonWorld(clock)
    run = await w.failed_run()
    assert w.owner is not None

    result = await _get(w, run, w.owner)

    assert result == TailoringRunFitScores(
        before=None,
        after=None,
        comparison=None,
        comparable=None,
        unavailable=ComparisonUnavailableReason.NOT_SUCCEEDED,
    )


async def test_a_deleted_base_cv_is_unavailable_and_hides_both_sides(clock: FixedClock) -> None:
    """S-6: the `GET` says `unavailable`; it does not raise."""
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    assert w.cv_id is not None
    w.seed_before(await w.before_pair(run), 50)
    await w.cvs.remove(w.cv_id, w.owner)

    result = await _get(w, run, w.owner)

    assert (result.before, result.after, result.comparison, result.comparable) == (
        None,
        None,
        None,
        None,
    )
    assert result.unavailable is ComparisonUnavailableReason.BASE_CV_DELETED


async def test_not_succeeded_is_reported_before_a_deleted_base_cv(clock: FixedClock) -> None:
    w = ComparisonWorld(clock)
    run = await w.failed_run()
    assert w.owner is not None
    assert w.cv_id is not None
    await w.cvs.remove(w.cv_id, w.owner)

    result = await _get(w, run, w.owner)

    assert result.unavailable is ComparisonUnavailableReason.NOT_SUCCEEDED


async def test_an_unknown_run_and_someone_elses_run_are_the_same_not_found(
    clock: FixedClock,
) -> None:
    w = ComparisonWorld(clock)
    run = await w.succeeded_run()
    assert w.owner is not None
    stranger = await w.user("stranger@example.com")

    with pytest.raises(TailoringRunNotFound) as unknown:
        await w.fit_scores()(
            GetTailoringRunFitScoresQuery(
                owner=w.owner, tailoring_run_id=TailoringRunId(value=uuid4())
            )
        )
    with pytest.raises(TailoringRunNotFound) as not_mine:
        await _get(w, run, stranger)

    assert type(unknown.value) is type(not_mine.value) is TailoringRunNotFound
