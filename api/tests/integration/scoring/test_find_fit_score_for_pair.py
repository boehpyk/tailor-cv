"""Application tests for `FindFitScoreForPair` (slice 5.1, T15, RED): the stored score of a pair, or none.

From the spec's AC-2, AC-3, AC-4, AC-9 and S-22 to S-24, and the plan's *Corrections* (S-24's `GET`
answers `None`). A stored hit **touches no scorer and no ledger** and counts against nothing: it is
asserted on the `journal` and the call logs, with the guest at their limit and the day's cap full, so
a use case that "helpfully" counted or reserved would show.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest

from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.posting.errors import JobPostingNotFound
from tailorcraft.domain.scoring.value_objects import MicroUsd
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.owners import extraction_failed_cv, pasted_posting, uploaded_cv
from tests.integration.scoring.support import (
    MAX_PER_SESSION,
    RESERVED,
    World,
    a_stored_score,
    extracted_cv_with,
    pair_of,
)

CV_TEXT = "Led a team of five engineers. Shipped Kubernetes migrations. " * 8


async def _stored_for(w: World, owner: Owner, cv_text: str = CV_TEXT):  # type: ignore[no-untyped-def]
    """A CV and posting for `owner`, the score already stored for exactly that pair, and the pair.

    The pair is built from the domain's own value objects, not by `ResolveScoringPair`, so a test of
    `for_pair` goes red on `for_pair` and not on its arrangement."""
    cv = extracted_cv_with(owner, w.earlier, cv_text)
    posting = pasted_posting(owner, w.earlier)
    await w.cvs.add(cv)
    await w.postings.add(posting)
    pair = pair_of(cv_text, posting.text.value)
    stored = a_stored_score(owner, w.earlier, pair=pair)
    w.scores.seed(stored)
    return cv, posting, stored, pair


def _assert_nothing_spent_or_called(w: World) -> None:
    assert w.scorer.calls == []
    assert w.ledger.reservations == []
    assert w.ledger.settlements == []
    assert w.scores.since_calls == []
    assert w.events.published == []


# --- GET: __call__ ------------------------------------------------------------------------------------------


async def test_a_stored_score_is_found_by_the_ids_of_its_pair(clock: FixedClock) -> None:
    w = World(clock)
    guest = await w.guest()
    cv, posting, stored, _ = await _stored_for(w, guest)

    found = await w.find()(guest, cv.id, posting.id)

    assert found is stored
    _assert_nothing_spent_or_called(w)


async def test_a_pair_never_scored_has_no_score(clock: FixedClock) -> None:
    w = World(clock)
    guest = await w.guest()
    cv_id, posting_id = await w.inputs(guest, cv_text=CV_TEXT)

    assert await w.find()(guest, cv_id, posting_id) is None
    _assert_nothing_spent_or_called(w)


async def test_a_reupload_of_the_same_text_finds_the_stored_score(clock: FixedClock) -> None:
    """AC-3: the key is the content, so a new CV row with the same words is the same pair."""
    w = World(clock)
    guest = await w.guest()
    cv, posting, stored, _ = await _stored_for(w, guest)
    again = extracted_cv_with(guest, w.earlier, CV_TEXT)
    await w.cvs.add(again)
    assert again.id != cv.id

    assert await w.find()(guest, again.id, posting.id) is stored


async def test_different_text_is_a_different_pair_with_no_score(clock: FixedClock) -> None:
    w = World(clock)
    guest = await w.guest()
    _, posting, _, _ = await _stored_for(w, guest)
    other = extracted_cv_with(guest, w.earlier, CV_TEXT + " One more achievement, here.")
    await w.cvs.add(other)

    assert await w.find()(guest, other.id, posting.id) is None


async def test_another_owners_score_of_the_same_text_is_not_found(clock: FixedClock) -> None:
    """AC-4: no score crosses owners, even for an identical CV and posting text."""
    w = World(clock)
    alice, bob = await w.guest("alice"), await w.guest("bob")
    await _stored_for(w, alice)
    bobs_cv = extracted_cv_with(bob, w.earlier, CV_TEXT)
    bobs_posting = pasted_posting(bob, w.earlier)
    await w.cvs.add(bobs_cv)
    await w.postings.add(bobs_posting)

    assert await w.find()(bob, bobs_cv.id, bobs_posting.id) is None


@pytest.mark.parametrize("build", [uploaded_cv, extraction_failed_cv], ids=["uploaded", "failed"])
async def test_a_get_for_a_cv_without_text_answers_none_not_an_error(
    clock: FixedClock, build: Callable[..., BaseCv]
) -> None:
    """S-24: a page that is only asking has done nothing wrong."""
    w = World(clock)
    guest = await w.guest()
    cv = build(guest, w.earlier)
    posting = pasted_posting(guest, w.earlier)
    await w.cvs.add(cv)
    await w.postings.add(posting)

    assert await w.find()(guest, cv.id, posting.id) is None


async def test_a_get_still_authorizes_the_cv(clock: FixedClock) -> None:
    w = World(clock)
    mine, theirs = await w.guest("mine"), await w.guest("theirs")
    _, posting, _, _ = await _stored_for(w, mine)
    their_cv = extracted_cv_with(theirs, w.earlier, CV_TEXT)
    await w.cvs.add(their_cv)

    with pytest.raises(BaseCvNotFound):
        await w.find()(mine, their_cv.id, posting.id)


async def test_a_get_still_authorizes_the_posting_before_looking_at_the_cvs_status(
    clock: FixedClock,
) -> None:
    w = World(clock)
    mine, theirs = await w.guest("mine"), await w.guest("theirs")
    cv = uploaded_cv(mine, w.earlier)
    their_posting = pasted_posting(theirs, w.earlier)
    await w.cvs.add(cv)
    await w.postings.add(their_posting)

    with pytest.raises(JobPostingNotFound):
        await w.find()(mine, cv.id, their_posting.id)


# --- POST: for_pair, before the limiter ----------------------------------------------------------------------


async def test_for_pair_returns_the_stored_score_by_the_pairs_fingerprints(
    clock: FixedClock,
) -> None:
    w = World(clock)
    guest = await w.guest()
    _, _, stored, pair = await _stored_for(w, guest)

    assert await w.find().for_pair(guest, pair) is stored


async def test_for_pair_of_a_new_pair_is_none(clock: FixedClock) -> None:
    w = World(clock)
    guest = await w.guest()
    await _stored_for(w, guest)

    assert await w.find().for_pair(guest, pair_of("a different cv " * 20)) is None


async def test_a_stored_hit_counts_against_nothing_even_at_the_limit_with_the_cap_full(
    clock: FixedClock,
) -> None:
    """AC-9: a session at 3 still gets its stored scores, with the cap reached."""
    w = World(clock)
    guest = await w.guest()
    _, _, stored, pair = await _stored_for(w, guest)
    w.seed_scores(guest, [clock.now() - timedelta(hours=h) for h in range(1, MAX_PER_SESSION)])
    assert len(w.scores.all()) == MAX_PER_SESSION
    w.ledger.spent[w.day] = 2_000_000

    hit = await w.find().for_pair(guest, pair)

    assert hit is stored
    _assert_nothing_spent_or_called(w)
    assert w.ledger.spent == {w.day: 2_000_000}


async def test_a_first_request_then_a_second_makes_one_call_and_the_second_finds_the_row(
    clock: FixedClock,
) -> None:
    """AC-2 at the use cases' level: the handler asks `for_pair`, then `RequestFitScore`; the next
    time `for_pair` finds the row and nothing is called or reserved."""
    w = World(clock)
    guest = await w.guest()
    pair = pair_of()
    assert await w.find().for_pair(guest, pair) is None

    first = await w.ask(guest, pair)
    reservations_after_first = list(w.ledger.reservations)
    second = await w.find().for_pair(guest, pair)

    assert first.created is True
    assert second is first.stored
    assert len(w.scorer.calls) == 1
    assert w.ledger.reservations == reservations_after_first
    assert w.ledger.reservations[0][1] == MicroUsd(RESERVED)
