"""Application tests for `GetTailoringRunForSession` and `ListTailoringRunsForSession` (T15, RED).

This is the slice's own version of the security test T12 wrote for `intake` and `posting`
(G-29/AC-14, ADR-0008/ADR-0010): "owning a session id is not authority over an object that
references it." `GetTailoringRunForSession` checks **the link** —
`run.guest_session_id == the resolved session id` — on every read, and a caller must not be able to
tell "exists but belongs to someone else" from "does not exist at all" from outside the use case,
because a distinguishable answer would confirm the id exists. This matters more here than for a CV
or a posting: a run id is the polling handle, so it is the id an attacker is most likely to be
enumerating (see `application/tailoring/get_tailoring_run.py`'s docstring).

Both branches raise the same `TailoringRunNotFound`, and the only place they are provably
distinguishable is `__cause__`: chained from `TailoringRunNotOwnedBySession` when a run exists but
belongs to someone else, chained from nothing at all when the id was never issued. Asserting both
halves of that pairing — not just the "wrong owner" half — is what proves the ownership check ran
at all, rather than the id simply being absent every time.

Same in-memory fakes as T9/T10/T12 (`FakeTailoringRunRepository`, `FakeGuestSessionRepository`,
`create_active_session`), imported from `tests/integration/fakes.py` rather than redefined — see
that module's docstring. Real Postgres is not used for the same reason `test_request_tailoring_run.py`
gives: there is no migration bringing `tailorcraft_test` to head yet, so testing against the ports
the use cases actually depend on is the honest red.

Every assertion below states what the use case **should** do per feature-spec.md's G-29/G-31/AC-14
and the two skeletons' own docstrings, never what the (currently `NotImplementedError`) code was
observed doing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRunForSession
from tailorcraft.application.tailoring.list_tailoring_runs import ListTailoringRunsForSession
from tailorcraft.domain.identity.errors import GuestSessionExpired, GuestSessionNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.tailoring.errors import TailoringRunNotFound, TailoringRunNotOwnedBySession
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeGuestSessionRepository,
    FakeTailoringRunRepository,
    create_active_session,
)

# --- Test helpers --------------------------------------------------------------------------------


async def _add_tailoring_run(
    runs: FakeTailoringRunRepository,
    session_id: GuestSessionId,
    clock: FixedClock,
) -> TailoringRun:
    run_id = runs.next_identity()
    run = TailoringRun.request(
        id=run_id,
        guest_session_id=session_id,
        base_cv_id=BaseCvId(value=uuid4()),
        job_posting_id=JobPostingId(value=uuid4()),
        requested_at=clock.now(),
    )
    await runs.add(run)
    return run


# --- GetTailoringRunForSession ---------------------------------------------------------------------


async def test_get_returns_the_run_for_the_session_that_owns_it(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()
    run = await _add_tailoring_run(runs, session.id, clock)

    use_case = GetTailoringRunForSession(runs, sessions, clock)
    result = await use_case(run.id, session.id)

    assert result.id == run.id
    assert result.guest_session_id == session.id


async def test_get_for_a_run_owned_by_a_different_session_raises_tailoring_run_not_found(
    clock: FixedClock,
) -> None:
    """G-29/AC-14: the run exists, but belongs to a session other than the caller's. The use case
    must raise the same `TailoringRunNotFound` a nonexistent id raises — see the sibling test below
    — never `TailoringRunNotOwnedBySession` or a 403-shaped signal, which would confirm to someone
    guessing ids that the id is real. The distinguishing fact survives only on `__cause__`, which is
    invisible to anything reading only the type that crosses the use-case boundary — asserting it
    explicitly is the only way to prove the ownership check ran rather than the id simply being
    absent."""
    sessions = FakeGuestSessionRepository()
    owner_session = await create_active_session(sessions, clock, token_hash="owner-session-hash-ab")
    other_session = await create_active_session(sessions, clock, token_hash="other-session-hash-cd")
    runs = FakeTailoringRunRepository()
    run = await _add_tailoring_run(runs, owner_session.id, clock)

    use_case = GetTailoringRunForSession(runs, sessions, clock)

    with pytest.raises(TailoringRunNotFound) as exc_info:
        await use_case(run.id, other_session.id)

    assert isinstance(exc_info.value.__cause__, TailoringRunNotOwnedBySession)


async def test_get_for_a_nonexistent_id_raises_tailoring_run_not_found_without_ownership_cause(
    clock: FixedClock,
) -> None:
    """The same exception type as the "wrong owner" case above — on purpose, a caller must not be
    able to distinguish "this id belongs to someone else" from "this id was never issued". But
    `__cause__` here must NOT be a `TailoringRunNotOwnedBySession`, in contrast with the previous
    test — that contrast is what proves the two branches are internally distinguishable at all,
    rather than this test accidentally passing because nothing ever sets `__cause__` to that type
    either way."""
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()  # empty: no run was ever added

    use_case = GetTailoringRunForSession(runs, sessions, clock)
    nonexistent_id = TailoringRunId(value=uuid4())

    with pytest.raises(TailoringRunNotFound) as exc_info:
        await use_case(nonexistent_id, session.id)

    assert not isinstance(exc_info.value.__cause__, TailoringRunNotOwnedBySession)


async def test_get_with_expired_session_raises_guest_session_expired(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    expired = GuestSession.start(
        id=sessions.next_identity(),
        token_hash="expired-session-hash-get".ljust(64, "0"),
        at=clock.now() - timedelta(hours=48),
        ttl_hours=24,
    )
    await sessions.add(expired)
    runs = FakeTailoringRunRepository()
    run = await _add_tailoring_run(runs, expired.id, clock)

    use_case = GetTailoringRunForSession(runs, sessions, clock)

    with pytest.raises(GuestSessionExpired):
        await use_case(run.id, expired.id)


async def test_get_with_unknown_session_raises_guest_session_not_found(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()  # empty: no session was ever added
    runs = FakeTailoringRunRepository()

    use_case = GetTailoringRunForSession(runs, sessions, clock)
    unknown_session_id = GuestSessionId(value=uuid4())
    some_run_id = TailoringRunId(value=uuid4())

    with pytest.raises(GuestSessionNotFound):
        await use_case(some_run_id, unknown_session_id)


# --- ListTailoringRunsForSession -------------------------------------------------------------------


async def test_list_returns_only_this_sessions_runs(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session_a = await create_active_session(
        sessions, clock, token_hash="session-a-hash".ljust(64, "0")
    )
    session_b = await create_active_session(
        sessions, clock, token_hash="session-b-hash".ljust(64, "0")
    )
    runs = FakeTailoringRunRepository()
    run_a1 = await _add_tailoring_run(runs, session_a.id, clock)
    run_a2 = await _add_tailoring_run(runs, session_a.id, clock)
    run_b1 = await _add_tailoring_run(runs, session_b.id, clock)

    use_case = ListTailoringRunsForSession(runs, sessions, clock)
    result = await use_case(session_a.id)

    result_ids = {run.id for run in result}
    assert result_ids == {run_a1.id, run_a2.id}
    # not merely a count check — session B's run must be absent by name, not just outnumbered
    assert run_b1.id not in result_ids


async def test_list_returns_runs_newest_first(clock: FixedClock) -> None:
    """`requested_at DESC` is the ordering under test, so the three runs need three distinct
    `requested_at` instants. The `Clock` port is whole-second by contract (ADR-0007): building all
    three at the same `clock.now()` would leave the ordering decided by the repository's `id DESC`
    tiebreak instead of by time, which is not what this test claims to exercise. `clock.advance(1)`
    between each `_add_tailoring_run` call gives each run its own second, so the assertion below is
    unambiguously about `requested_at` and not about the tiebreak."""
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()

    oldest = await _add_tailoring_run(runs, session.id, clock)
    clock.advance(1)
    middle = await _add_tailoring_run(runs, session.id, clock)
    clock.advance(1)
    newest = await _add_tailoring_run(runs, session.id, clock)

    use_case = ListTailoringRunsForSession(runs, sessions, clock)
    result = await use_case(session.id)

    assert [run.id for run in result] == [newest.id, middle.id, oldest.id]


async def test_list_returns_empty_sequence_when_session_owns_none(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    runs = FakeTailoringRunRepository()  # empty: this session owns nothing

    use_case = ListTailoringRunsForSession(runs, sessions, clock)
    result = await use_case(session.id)

    assert isinstance(result, Sequence)
    assert result is not None
    assert len(result) == 0


async def test_list_with_expired_session_raises_guest_session_expired(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    expired = GuestSession.start(
        id=sessions.next_identity(),
        token_hash="expired-session-hash-list".ljust(64, "0"),
        at=clock.now() - timedelta(hours=48),
        ttl_hours=24,
    )
    await sessions.add(expired)
    runs = FakeTailoringRunRepository()

    use_case = ListTailoringRunsForSession(runs, sessions, clock)

    with pytest.raises(GuestSessionExpired):
        await use_case(expired.id)


async def test_list_with_unknown_session_raises_guest_session_not_found(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()  # empty: no session was ever added
    runs = FakeTailoringRunRepository()

    use_case = ListTailoringRunsForSession(runs, sessions, clock)
    unknown_session_id = GuestSessionId(value=uuid4())

    with pytest.raises(GuestSessionNotFound):
        await use_case(unknown_session_id)
