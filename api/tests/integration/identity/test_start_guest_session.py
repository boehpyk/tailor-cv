"""Application tests for `StartGuestSession` (T12, RED).

Covers what `application/identity/start_guest_session.py`'s docstring commits to: mint and persist
a `GuestSession` bound to a `token_hash` the caller already computed (the plaintext token never
reaches this layer — ADR-0010), with `expires_at = now + retention_hours` on the whole second the
`Clock` port guarantees (ADR-0007).

Same `FakeGuestSessionRepository` as T9/T10's `UploadBaseCv` tests and T12's read-side tests,
imported from `tests/integration/fakes.py` rather than redefined — see that module's docstring.
No real Postgres for the same reason those modules give: there is no migration yet bringing
`tailorcraft_test` to head, so this is the honest red against the port the use case depends on.

Every assertion states what `StartGuestSession.__call__` should do per its own docstring, never
what the (currently `NotImplementedError`) code was observed doing.
"""

from __future__ import annotations

from datetime import timedelta

from tailorcraft.application.identity.start_guest_session import StartGuestSession
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import FakeGuestSessionRepository


async def test_sets_expires_at_to_now_plus_retention_hours_on_the_whole_second(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    use_case = StartGuestSession(sessions, clock, retention_hours=24)

    session = await use_case(token_hash="e" * 64)

    assert session.expires_at == clock.now() + timedelta(hours=24)
    # whole-second per the Clock contract (ADR-0007) — a FixedClock cannot introduce a fractional
    # component, so this would only fail if the use case itself did arithmetic that could.
    assert session.expires_at.microsecond == 0


async def test_persists_the_session_findable_afterwards_by_its_token_hash(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    use_case = StartGuestSession(sessions, clock, retention_hours=24)

    session = await use_case(token_hash="f" * 64)

    found = await sessions.find_by_token_hash("f" * 64)
    assert found is not None
    assert found.id == session.id


async def test_stores_the_token_hash_it_was_given_unchanged(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    use_case = StartGuestSession(sessions, clock, retention_hours=24)
    given_hash = "g" * 64

    session = await use_case(token_hash=given_hash)

    assert session.token_hash == given_hash


async def test_returns_the_real_guest_session_aggregate(clock: FixedClock) -> None:
    """Not a narrower DTO — the caller (the cookie-resolution dependency) needs both `id` and
    `expires_at` off the returned object, per the use case's own docstring."""
    sessions = FakeGuestSessionRepository()
    use_case = StartGuestSession(sessions, clock, retention_hours=24)

    session = await use_case(token_hash="h" * 64)

    assert isinstance(session, GuestSession)
