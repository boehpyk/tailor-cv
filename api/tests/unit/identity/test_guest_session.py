"""`GuestSession`: an expiring, opaque session and the one invariant it carries.

Pure domain tests: no I/O, no fixtures, no event loop, no mocks. Time comes from `FixedClock` only,
never `datetime.now()` — a session's `is_expired` is exactly the kind of check that becomes flaky
under the real clock if a test is ever slow to run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.infrastructure.clock import FixedClock

_SESSION_ID = GuestSessionId(value=UUID("11111111-1111-7111-8111-111111111111"))
_TOKEN_HASH = "a" * 64  # a SHA-256 hex digest is 64 characters; the exact value never matters here


def test_start_sets_expires_at_ttl_hours_after_created_at_on_a_whole_second() -> None:
    """`created_at` is whatever `Clock` handed `start`; `expires_at` is `ttl_hours` later. Both
    come from `FixedClock`, which is itself whole-second by contract (ADR-0007), so this also
    proves `start` does not introduce a sub-second drift of its own."""
    clock = FixedClock(datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC))

    session = GuestSession.start(_SESSION_ID, _TOKEN_HASH, clock.now(), ttl_hours=24)

    assert session.created_at == clock.now()
    assert session.expires_at == clock.now() + timedelta(hours=24)
    assert session.expires_at.microsecond == 0


@pytest.mark.parametrize("ttl_hours", [0, -1], ids=["zero", "negative"])
def test_start_rejects_a_non_positive_ttl_hours(ttl_hours: int) -> None:
    """Invariant: `expires_at > created_at`. A non-positive TTL would produce a session that is
    already expired the instant it is created, or expires at the instant it starts — neither is a
    session, so `start` must refuse to build one."""
    clock = FixedClock(datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC))

    with pytest.raises(InvariantViolated):
        GuestSession.start(_SESSION_ID, _TOKEN_HASH, clock.now(), ttl_hours=ttl_hours)


# --- is_expired: inclusive at the boundary ------------------------------------------------------
#
# The reading asserted below — `is_expired` is True *at* `expires_at`, not only strictly after it —
# is not stated in the feature-spec or the technical-plan; it comes from the skeleton's own
# docstring ("Whether `at` is at or past `expires_at`"), which already commits to "at or past"
# rather than "strictly past". That is the reading these tests hold the implementer to: a guest
# whose cookie is checked in the same whole second the session dies should not get one more request
# through on a technicality. If the boundary is ever revisited, it must be revisited on purpose, in
# this docstring and the skeleton's together, not left to whichever comparison operator someone
# happens to type in GREEN.


def test_is_expired_returns_true_at_the_exact_expiry_instant() -> None:
    clock = FixedClock(datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC))
    session = GuestSession.start(_SESSION_ID, _TOKEN_HASH, clock.now(), ttl_hours=24)

    assert session.is_expired(session.expires_at) is True


def test_is_expired_returns_false_one_second_before_expiry() -> None:
    clock = FixedClock(datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC))
    session = GuestSession.start(_SESSION_ID, _TOKEN_HASH, clock.now(), ttl_hours=24)
    one_second_before = session.expires_at - timedelta(seconds=1)

    assert session.is_expired(one_second_before) is False


def test_is_expired_returns_true_one_second_after_expiry() -> None:
    clock = FixedClock(datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC))
    session = GuestSession.start(_SESSION_ID, _TOKEN_HASH, clock.now(), ttl_hours=24)
    one_second_after = session.expires_at + timedelta(seconds=1)

    assert session.is_expired(one_second_after) is True
