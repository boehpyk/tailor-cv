"""Application tests for `ClaimGuestWork` (T13, RED, slice 2.4, AC-2 and AC-3).

Written from the spec's acceptance criteria, not from the code. The destination user is the **real**
`SqlAlchemyUserRepository` over the rolled-back test database (a mocked repository would test the
mock, and "an erased user" is only meaningful against a table that really lacks the row); the claim
port and the file store are recording doubles, because the order and the *absence* of calls are the
claims.

**Why `_attempt`.** Against the skeleton `__call__` raises `NotImplementedError`. A bare
`pytest.raises(UserNotFound)` would turn that into an *error*, not a failed assertion, and
`NotImplementedError` subclasses `RuntimeError`, so a looser `raises` would pass vacuously.
`_attempt` returns what the call produced (report or exception instance), and every test asserts on
it with an exact `type(...) is`, so a skeleton fails on an assertion that names what was expected.

**Absence is paired with a positive.** "The port was never called" is trivially true of a skeleton,
so each such test also asserts the outcome (`nothing()` or `UserNotFound`) and, where the spec
promises one, the call that *did* happen.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.identity.claim_guest_work import ClaimGuestWork
from tailorcraft.domain.identity.claim import ClaimedGuestWork, GuestWorkClaimReport
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    GuestSessionId,
    PasswordHash,
    UserId,
)
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.persistence.mapping.identity import (
    user as _user_mapping,  # noqa: F401
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.integration.fakes import InMemoryFileStore, RecordingGuestWorkClaim

_TOKEN_HASH = "b" * 64
_HASH = PasswordHash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA")


class _OrderedFileStore(InMemoryFileStore):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    async def delete(self, ref: FileRef) -> None:
        self._order.append("delete")
        await super().delete(ref)


def _ref() -> FileRef:
    return FileRef.for_base_cv(BaseCvId(value=uuid4()), CvContentType.PDF)


def _session(clock: FixedClock, *, ttl_hours: int = 24) -> GuestSession:
    return GuestSession.start(
        id=GuestSessionId(value=uuid4()),
        token_hash=_TOKEN_HASH,
        at=clock.now(),
        ttl_hours=ttl_hours,
    )


def _claimed(files: tuple[FileRef, ...] = ()) -> ClaimedGuestWork:
    return ClaimedGuestWork(
        base_cvs=1,
        job_postings=2,
        tailoring_runs=3,
        export_jobs=4,
        working_copies_dropped=len(files),
        files_to_unlink=files,
    )


async def _real_user(session: AsyncSession, clock: FixedClock) -> UserId:
    users = SqlAlchemyUserRepository(session)
    user_id = users.next_identity()
    await users.add(
        User.register_with_password(
            id=user_id,
            email=EmailAddress.parse(f"{uuid4().hex[:12]}@example.com"),
            password_hash=_HASH,
            at=clock.now(),
        )
    )
    return user_id


async def _attempt(
    use_case: ClaimGuestWork, user_id: UserId, token_hash: str | None
) -> GuestWorkClaimReport | Exception:
    try:
        return await use_case(user_id, token_hash)
    except Exception as exc:
        return exc


async def test_an_erased_user_is_refused_before_the_claim_port_is_touched(
    session: AsyncSession, clock: FixedClock
) -> None:
    """AC-2: the user is resolved first, even with a live cookie for a live session."""
    claims = RecordingGuestWorkClaim(_session(clock), _claimed())
    files = InMemoryFileStore()
    use_case = ClaimGuestWork(SqlAlchemyUserRepository(session), claims, files, clock)

    outcome = await _attempt(use_case, UserId(value=uuid4()), _TOKEN_HASH)

    assert type(outcome) is UserNotFound
    assert claims.calls == []
    assert files.delete_calls == []


async def test_no_token_reports_nothing_and_never_touches_the_claim_port(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _real_user(session, clock)
    claims = RecordingGuestWorkClaim(_session(clock), _claimed())
    use_case = ClaimGuestWork(SqlAlchemyUserRepository(session), claims, InMemoryFileStore(), clock)

    outcome = await _attempt(use_case, user_id, None)

    assert outcome == GuestWorkClaimReport.nothing()
    assert claims.calls == []


async def test_a_token_matching_no_session_reports_nothing_and_never_transfers(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _real_user(session, clock)
    claims = RecordingGuestWorkClaim(None, _claimed())
    files = InMemoryFileStore()
    use_case = ClaimGuestWork(SqlAlchemyUserRepository(session), claims, files, clock)

    outcome = await _attempt(use_case, user_id, _TOKEN_HASH)

    assert outcome == GuestWorkClaimReport.nothing()
    # the positive half: the port WAS asked, with the hash it was given
    assert claims.calls == [("lock_session", _TOKEN_HASH)]
    assert files.delete_calls == []


async def test_an_expired_session_reports_nothing_and_never_transfers(
    session: AsyncSession, clock: FixedClock
) -> None:
    """AC-2: an expired session's work is the purge's. Time comes from the fake clock."""
    user_id = await _real_user(session, clock)
    guest = _session(clock, ttl_hours=24)
    clock.advance(int(timedelta(hours=25).total_seconds()))
    assert guest.is_expired(clock.now())  # the precondition the test relies on
    claims = RecordingGuestWorkClaim(guest, _claimed((_ref(),)))
    files = InMemoryFileStore()
    use_case = ClaimGuestWork(SqlAlchemyUserRepository(session), claims, files, clock)

    outcome = await _attempt(use_case, user_id, _TOKEN_HASH)

    assert outcome == GuestWorkClaimReport.nothing()
    assert claims.calls == [("lock_session", _TOKEN_HASH)]
    assert files.delete_calls == []


async def test_a_live_session_is_locked_then_transferred_then_each_file_unlinked_in_that_order(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _real_user(session, clock)
    guest = _session(clock)
    refs = (_ref(), _ref(), _ref())
    order: list[str] = []
    claims = RecordingGuestWorkClaim(guest, _claimed(refs), order=order)
    files = _OrderedFileStore(order)
    use_case = ClaimGuestWork(SqlAlchemyUserRepository(session), claims, files, clock)

    outcome = await _attempt(use_case, user_id, _TOKEN_HASH)

    assert outcome == GuestWorkClaimReport(
        base_cvs=1,
        job_postings=2,
        tailoring_runs=3,
        export_jobs=4,
        working_copies_dropped=3,
        files_unlinked=3,
        unlink_failures=(),
    )
    assert order == ["lock_session", "transfer", "delete", "delete", "delete"]
    assert claims.calls == [
        ("lock_session", _TOKEN_HASH),
        ("transfer", guest.id, user_id),
    ]
    assert files.delete_calls == list(refs)


async def test_a_failing_unlink_is_returned_by_type_name_and_the_loop_continues(
    session: AsyncSession, clock: FixedClock
) -> None:
    """C-31: returned, never raised, and the next file is still tried."""
    user_id = await _real_user(session, clock)
    first, failing, last = _ref(), _ref(), _ref()
    claims = RecordingGuestWorkClaim(_session(clock), _claimed((first, failing, last)))
    files = InMemoryFileStore(
        fail_delete=FileStoreUnavailable("EIO at /secret/path — must not be returned"),
        fail_delete_keys={failing.key},
    )
    use_case = ClaimGuestWork(SqlAlchemyUserRepository(session), claims, files, clock)

    outcome = await _attempt(use_case, user_id, _TOKEN_HASH)

    assert isinstance(outcome, GuestWorkClaimReport), outcome
    assert outcome.unlink_failures == ("FileStoreUnavailable",)
    assert outcome.files_unlinked == 2
    assert files.delete_calls == [first, failing, last]  # the loop did not stop at the failure


async def test_a_user_erased_mid_claim_propagates_and_nothing_is_unlinked(
    session: AsyncSession, clock: FixedClock
) -> None:
    """AC-3: `transfer` raising `UserNotFound` is not swallowed, and no file is touched."""
    user_id = await _real_user(session, clock)
    guest = _session(clock)
    claims = RecordingGuestWorkClaim(
        guest, _claimed((_ref(),)), transfer_error=UserNotFound("erased between read and re-key")
    )
    files = InMemoryFileStore()
    use_case = ClaimGuestWork(SqlAlchemyUserRepository(session), claims, files, clock)

    outcome = await _attempt(use_case, user_id, _TOKEN_HASH)

    assert type(outcome) is UserNotFound
    # the positive half: the use case really reached `transfer` before it failed
    assert claims.calls == [("lock_session", _TOKEN_HASH), ("transfer", guest.id, user_id)]
    assert files.delete_calls == []


async def test_the_use_case_logs_nothing_even_when_an_unlink_fails(
    session: AsyncSession, clock: FixedClock, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-2: zero records from `application/`. Filtered to the project's loggers so SQLAlchemy's own
    DEBUG chatter on the real user lookup is not counted; paired with a real failure so the absence
    is not vacuous."""
    configure_logging(settings)
    user_id = await _real_user(session, clock)
    ref = _ref()
    claims = RecordingGuestWorkClaim(_session(clock), _claimed((ref,)))
    files = InMemoryFileStore(fail_delete=FileStoreUnavailable("EIO must never reach a log line"))
    use_case = ClaimGuestWork(SqlAlchemyUserRepository(session), claims, files, clock)

    with caplog.at_level("DEBUG"):
        outcome = await _attempt(use_case, user_id, _TOKEN_HASH)

    assert isinstance(outcome, GuestWorkClaimReport), outcome
    assert outcome.unlink_failures == ("FileStoreUnavailable",)
    assert [r for r in caplog.records if r.name.startswith("tailorcraft")] == []
