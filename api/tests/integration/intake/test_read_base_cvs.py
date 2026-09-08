"""Application tests for `GetBaseCvForSession` and `ListBaseCvsForSession` (T12, RED).

This is the slice's security test (AC-8/F-20, ADR-0008): "owning a session id is not authority over
an object that references it." Both use cases check **the link** — `cv.guest_session_id == the
resolved session id` — on every read, and a caller must not be able to tell "exists but belongs to
someone else" from "does not exist at all", because a distinguishable answer would confirm the id
exists. See `application/intake/get_base_cv.py`'s docstring for the exact contract this file tests
against: `BaseCvNotFound` for both cases, chained from `BaseCvNotOwnedBySession` (visible on
`__cause__`) only in the "wrong owner" branch.

Same in-memory fakes as T9/T10 (`UploadBaseCv`), imported from `tests/integration/fakes.py` rather
than redefined — see that module's docstring. Real Postgres is not used for the same reason T9's
module docstring gives: as of this commit there is no migration bringing `tailorcraft_test` to
head, so testing against the ports the use cases actually depend on is the honest red. T28 covers
the real persistence round-trip once the repositories exist.

Every assertion states what the use case **should** do per technical-plan.md's "Use cases"
paragraph and feature-spec.md's AC-8/AC-9/F-19/F-20, never what the (currently `NotImplementedError`)
code was observed doing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.intake.get_base_cv import GetBaseCvForSession
from tailorcraft.application.intake.list_base_cvs import ListBaseCvsForSession
from tailorcraft.domain.identity.errors import GuestSessionExpired, GuestSessionNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound, BaseCvNotOwnedBySession
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType, OriginalFilename
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeBaseCvRepository,
    FakeGuestSessionRepository,
    create_active_session,
)

# --- Test helpers --------------------------------------------------------------------------------


async def _add_base_cv(
    cvs: FakeBaseCvRepository,
    session_id: GuestSessionId,
    clock: FixedClock,
    *,
    filename: str = "cv.pdf",
) -> BaseCv:
    cv_id = cvs.next_identity()
    cv = BaseCv.upload(
        id=cv_id,
        guest_session_id=session_id,
        original_filename=OriginalFilename(filename),
        content_type=CvContentType.PDF,
        size_bytes=10,
        file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
        uploaded_at=clock.now(),
    )
    await cvs.add(cv)
    return cv


# --- GetBaseCvForSession ---------------------------------------------------------------------------


async def test_get_returns_the_cv_for_the_session_that_owns_it(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    cvs = FakeBaseCvRepository()
    cv = await _add_base_cv(cvs, session.id, clock)

    use_case = GetBaseCvForSession(cvs, sessions, clock)
    result = await use_case(cv.id, session.id)

    assert result.id == cv.id
    assert result.guest_session_id == session.id


async def test_get_for_a_cv_owned_by_a_different_session_raises_base_cv_not_found(
    clock: FixedClock,
) -> None:
    """F-20/AC-8: the base CV exists, but belongs to a session other than the caller's. The use
    case must raise the same `BaseCvNotFound` a nonexistent id raises — see the sibling test below
    — never `BaseCvNotOwnedBySession` or a 403-shaped signal, which would confirm the id exists."""
    sessions = FakeGuestSessionRepository()
    owner_session = await create_active_session(sessions, clock, token_hash="owner" * 13)
    other_session = await create_active_session(sessions, clock, token_hash="other" * 13)
    cvs = FakeBaseCvRepository()
    cv = await _add_base_cv(cvs, owner_session.id, clock)

    use_case = GetBaseCvForSession(cvs, sessions, clock)

    with pytest.raises(BaseCvNotFound) as exc_info:
        await use_case(cv.id, other_session.id)

    # The distinguishing detail lives on __cause__, visible only to this test — not to anything
    # reading the exception type that actually crosses the use-case boundary (the docstring's
    # explicit contract: "not mine" is a BaseCvNotFound chained from BaseCvNotOwnedBySession).
    assert isinstance(exc_info.value.__cause__, BaseCvNotOwnedBySession)


async def test_get_for_a_nonexistent_id_raises_base_cv_not_found(clock: FixedClock) -> None:
    """The same exception type as the "wrong owner" case above — on purpose. A caller must not be
    able to distinguish "this id belongs to someone else" from "this id was never issued"."""
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    cvs = FakeBaseCvRepository()  # empty: no base CV was ever added

    use_case = GetBaseCvForSession(cvs, sessions, clock)
    nonexistent_id = BaseCvId(value=uuid4())

    with pytest.raises(BaseCvNotFound):
        await use_case(nonexistent_id, session.id)


async def test_get_with_expired_session_raises_guest_session_expired(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    expired = GuestSession.start(
        id=sessions.next_identity(),
        token_hash="c" * 64,
        at=clock.now() - timedelta(hours=48),
        ttl_hours=24,
    )
    await sessions.add(expired)
    cvs = FakeBaseCvRepository()
    cv = await _add_base_cv(cvs, expired.id, clock)

    use_case = GetBaseCvForSession(cvs, sessions, clock)

    with pytest.raises(GuestSessionExpired):
        await use_case(cv.id, expired.id)


async def test_get_with_unknown_session_raises_guest_session_not_found(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()  # empty: no session was ever added
    cvs = FakeBaseCvRepository()

    use_case = GetBaseCvForSession(cvs, sessions, clock)
    unknown_session_id = GuestSessionId(value=uuid4())
    some_cv_id = BaseCvId(value=uuid4())

    with pytest.raises(GuestSessionNotFound):
        await use_case(some_cv_id, unknown_session_id)


# --- ListBaseCvsForSession -------------------------------------------------------------------------


async def test_list_returns_only_this_sessions_cvs(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session_a = await create_active_session(sessions, clock, token_hash="a-session" * 7)
    session_b = await create_active_session(sessions, clock, token_hash="b-session" * 7)
    cvs = FakeBaseCvRepository()
    cv_a1 = await _add_base_cv(cvs, session_a.id, clock, filename="a1.pdf")
    cv_a2 = await _add_base_cv(cvs, session_a.id, clock, filename="a2.pdf")
    cv_b1 = await _add_base_cv(cvs, session_b.id, clock, filename="b1.pdf")

    use_case = ListBaseCvsForSession(cvs, sessions, clock)
    result = await use_case(session_a.id)

    result_ids = {cv.id for cv in result}
    assert result_ids == {cv_a1.id, cv_a2.id}
    # not merely a count check — session B's CV must be absent by name, not just outnumbered
    assert cv_b1.id not in result_ids


async def test_list_returns_empty_sequence_when_session_owns_none(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    cvs = FakeBaseCvRepository()  # empty: this session owns nothing

    use_case = ListBaseCvsForSession(cvs, sessions, clock)
    result = await use_case(session.id)

    assert isinstance(result, Sequence)
    assert result is not None
    assert len(result) == 0


async def test_list_with_expired_session_raises_guest_session_expired(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    expired = GuestSession.start(
        id=sessions.next_identity(),
        token_hash="d" * 64,
        at=clock.now() - timedelta(hours=48),
        ttl_hours=24,
    )
    await sessions.add(expired)
    cvs = FakeBaseCvRepository()

    use_case = ListBaseCvsForSession(cvs, sessions, clock)

    with pytest.raises(GuestSessionExpired):
        await use_case(expired.id)


async def test_list_with_unknown_session_raises_guest_session_not_found(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()  # empty: no session was ever added
    cvs = FakeBaseCvRepository()

    use_case = ListBaseCvsForSession(cvs, sessions, clock)
    unknown_session_id = GuestSessionId(value=uuid4())

    with pytest.raises(GuestSessionNotFound):
        await use_case(unknown_session_id)
