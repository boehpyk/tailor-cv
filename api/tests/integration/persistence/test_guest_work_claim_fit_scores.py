"""The claim's new statement against a real Postgres (slice 5.1, T21, **red first**; AC-34, AC-35,
plan §3.3). The statement is `SqlAlchemyGuestWorkClaim.transfer`'s fifth: it moves a guest session's
fit scores to the user, **except** a score whose CV is one of the session's working copies and a score
whose pair the user already has. Both leftovers go with the session's own cascade, so there is no
`DELETE` for them.

Written from the spec: a working copy is a copy of a CV some account already keeps, possibly another
person's, so a score made on it is never claimed (AC-34); a twin is the same owner and the same pair
twice (AC-4's identity), so the user's row stays and the guest's does not move (AC-34); the count
excludes both. Every outcome is read from the table after the claim, so the tests do not depend on
how the statement is written.

**A claim that collides is not these tests'**: a twin that appears *during* the claim is S-31
(`tests/api/test_guest_work_claim_fit_scores_http.py`). Here the twin is committed before the claim
starts, which the statement must see and skip rather than raise on.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.claim import ClaimedGuestWork
from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.value_objects import BaseCvLabel
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.identity.guest_work_claim import (
    SqlAlchemyGuestWorkClaim,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tests.integration.owners import extracted_cv
from tests.integration.persistence.fit_score_rows import insert_score_row, owner_of, score_of
from tests.integration.persistence.owner_rows import persist_guest, persist_user
from tests.integration.working_copy_support import seed_working_copy


class _Scene:
    """A guest with a saved CV and a working copy of someone else's CV, plus the user who claims."""

    def __init__(
        self, guest: GuestOwner, user: UserId, own_cv: UUID, copy_cv: UUID, clock: FixedClock
    ) -> None:
        self.guest = guest
        self.user = user
        self.own_cv = own_cv
        self.copy_cv = copy_cv
        self.at = clock.now() - timedelta(minutes=5)


async def _scene(session: AsyncSession, clock: FixedClock) -> _Scene:
    guest = await persist_guest(session, clock)
    owner_of_source = await persist_user(session, clock)
    claimant = await persist_user(session, clock)
    at = clock.now()
    cvs = SqlAlchemyBaseCvRepository(session)
    own = extracted_cv(guest, at)
    await cvs.add(own)
    source = extracted_cv(owner_of_source, at)
    source.rename(BaseCvLabel("Someone else's CV"), at)
    source.release_events()
    await cvs.add(source)
    copy = await seed_working_copy(session, guest, at, copied_from=source.id)
    await session.commit()
    return _Scene(guest, claimant.user_id, own.id.value, copy.id.value, clock)


async def _claim(session: AsyncSession, scene: _Scene) -> ClaimedGuestWork:
    """`transfer`, with a unique violation turned into a failure that says so: a claim that cannot
    skip a twin is the thing under test, and a bare `IntegrityError` would read as an error in the
    harness instead of as the assertion it is."""
    try:
        return await SqlAlchemyGuestWorkClaim(session).transfer(
            scene.guest.guest_session_id, scene.user
        )
    except IntegrityError as exc:
        pytest.fail(f"the claim collided on {violated_constraint(exc)} instead of skipping the row")


async def _fit_scores_claimed(claimed: ClaimedGuestWork) -> int | None:
    # `getattr`: T22 adds the field. A missing one must read as a wrong number, not an AttributeError.
    value = getattr(claimed, "fit_scores", None)
    return value if isinstance(value, int) else None


# --- the working-copy clause ----------------------------------------------------------------------


async def test_a_score_made_on_a_saved_cv_is_claimed(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The control for the clauses below: an ordinary score moves to the user, and only the owner
    changes."""
    scene = await _scene(session, clock)
    kept = await insert_score_row(
        session,
        guest_session_id=scene.guest.guest_session_id.value,
        base_cv_id=scene.own_cv,
        pair=1,
        score=71,
        scored_at=scene.at,
    )

    await _claim(session, scene)

    assert await owner_of(session, kept) == (None, scene.user.value)
    assert await score_of(session, kept) == 71


async def test_a_score_made_on_a_working_copy_is_not_claimed(
    session: AsyncSession, clock: FixedClock
) -> None:
    """AC-34. A working copy is dropped, never claimed, and a score about it must not become the
    account's: the copy may be a copy of another person's CV."""
    scene = await _scene(session, clock)
    on_copy = await insert_score_row(
        session,
        guest_session_id=scene.guest.guest_session_id.value,
        base_cv_id=scene.copy_cv,
        pair=2,
        scored_at=scene.at,
    )

    await _claim(session, scene)

    assert await owner_of(session, on_copy) != (None, scene.user.value), (
        "a working copy's score was moved into the account"
    )


async def test_a_score_made_on_a_working_copy_goes_with_the_session(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The leftover is deleted by the session's cascade: no row remains for anyone."""
    scene = await _scene(session, clock)
    on_copy = await insert_score_row(
        session,
        guest_session_id=scene.guest.guest_session_id.value,
        base_cv_id=scene.copy_cv,
        pair=2,
        scored_at=scene.at,
    )

    await _claim(session, scene)

    assert await owner_of(session, on_copy) is None


async def test_the_working_copy_clause_leaves_a_saved_cvs_score_in_the_same_claim(
    session: AsyncSession, clock: FixedClock
) -> None:
    """Both kinds in one claim: the clause is per row, not per session."""
    scene = await _scene(session, clock)
    saved = await insert_score_row(
        session,
        guest_session_id=scene.guest.guest_session_id.value,
        base_cv_id=scene.own_cv,
        pair=1,
        scored_at=scene.at,
    )
    on_copy = await insert_score_row(
        session,
        guest_session_id=scene.guest.guest_session_id.value,
        base_cv_id=scene.copy_cv,
        pair=2,
        scored_at=scene.at,
    )

    await _claim(session, scene)

    assert await owner_of(session, saved) == (None, scene.user.value)
    assert await owner_of(session, on_copy) is None


# --- the twin clause ------------------------------------------------------------------------------


async def test_a_claim_with_a_twin_pair_does_not_fail_and_keeps_the_users_row_untouched(
    session: AsyncSession, clock: FixedClock
) -> None:
    """AC-34. The user already has a score for this pair (from an earlier sign-in, say). The guest's
    twin must be skipped, not collided with, and the user's row is neither replaced nor rewritten."""
    scene = await _scene(session, clock)
    users = await insert_score_row(
        session,
        user_id=scene.user.value,
        base_cv_id=scene.own_cv,
        pair=3,
        score=90,
        scored_at=scene.at,
    )
    await insert_score_row(
        session,
        guest_session_id=scene.guest.guest_session_id.value,
        base_cv_id=scene.own_cv,
        pair=3,
        score=20,
        scored_at=scene.at,
    )

    await _claim(session, scene)

    assert await owner_of(session, users) == (None, scene.user.value)
    assert await score_of(session, users) == 90


async def test_the_guests_twin_goes_with_the_session_and_nothing_else_of_the_users_changes(
    session: AsyncSession, clock: FixedClock
) -> None:
    scene = await _scene(session, clock)
    users = await insert_score_row(
        session, user_id=scene.user.value, pair=3, score=90, scored_at=scene.at
    )
    twin = await insert_score_row(
        session,
        guest_session_id=scene.guest.guest_session_id.value,
        base_cv_id=scene.own_cv,
        pair=3,
        score=20,
        scored_at=scene.at,
    )
    other = await insert_score_row(
        session,
        guest_session_id=scene.guest.guest_session_id.value,
        base_cv_id=scene.own_cv,
        pair=4,
        score=33,
        scored_at=scene.at,
    )

    await _claim(session, scene)

    assert await owner_of(session, twin) is None
    assert await owner_of(session, other) == (None, scene.user.value), (
        "a twin must not stop the guest's other scores from moving"
    )
    assert await owner_of(session, users) == (None, scene.user.value)


async def test_a_pair_the_user_has_under_a_different_cv_is_still_a_twin(
    session: AsyncSession, clock: FixedClock
) -> None:
    """Identity is the content, not the CV row (AC-3): the user's score for the same text on another
    CV id is the same pair."""
    scene = await _scene(session, clock)
    users = await insert_score_row(
        session, user_id=scene.user.value, pair=3, score=90, scored_at=scene.at
    )
    twin = await insert_score_row(
        session,
        guest_session_id=scene.guest.guest_session_id.value,
        base_cv_id=scene.own_cv,
        pair=3,
        score=20,
        scored_at=scene.at,
    )

    await _claim(session, scene)

    assert await owner_of(session, twin) is None
    assert await score_of(session, users) == 90


# --- the count ------------------------------------------------------------------------------------


async def test_the_claim_counts_the_scores_it_moved_and_none_it_skipped(
    session: AsyncSession, clock: FixedClock
) -> None:
    """One moves, one is a twin, one is on a working copy: the count is 1."""
    scene = await _scene(session, clock)
    await insert_score_row(
        session,
        guest_session_id=scene.guest.guest_session_id.value,
        base_cv_id=scene.own_cv,
        pair=1,
        scored_at=scene.at,
    )
    await insert_score_row(session, user_id=scene.user.value, pair=3, scored_at=scene.at)
    await insert_score_row(
        session,
        guest_session_id=scene.guest.guest_session_id.value,
        base_cv_id=scene.own_cv,
        pair=3,
        scored_at=scene.at,
    )
    await insert_score_row(
        session,
        guest_session_id=scene.guest.guest_session_id.value,
        base_cv_id=scene.copy_cv,
        pair=2,
        scored_at=scene.at,
    )

    claimed = await _claim(session, scene)

    assert await _fit_scores_claimed(claimed) == 1


async def test_the_claim_counts_every_score_it_moved(
    session: AsyncSession, clock: FixedClock
) -> None:
    scene = await _scene(session, clock)
    for pair in (1, 2, 3):
        await insert_score_row(
            session,
            guest_session_id=scene.guest.guest_session_id.value,
            base_cv_id=scene.own_cv,
            pair=pair,
            scored_at=scene.at,
        )

    claimed = await _claim(session, scene)

    assert await _fit_scores_claimed(claimed) == 3


async def test_a_claim_of_a_session_with_no_scores_counts_zero(
    session: AsyncSession, clock: FixedClock
) -> None:
    scene = await _scene(session, clock)

    claimed = await _claim(session, scene)

    assert await _fit_scores_claimed(claimed) == 0
