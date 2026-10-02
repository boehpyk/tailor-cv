"""Application tests for `RequestPasswordReset` (slice 2.5, T13 RED — AC-8, V-40, V-41).

Same two-proof shape as `test_request_registration.py`, for the same reason: the claim is that the
request has no branch on the address and **no hash at all**. The constructor is given neither a
`UserRepository` nor a `PasswordHasherPort` (structural, green against the skeleton, red under the
AC-40 mutation), and the positives — exactly one `resets.add` of an *addressed* reset, then exactly
one enqueue of its id — are what go red.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tailorcraft.application.identity.request_password_reset import RequestPasswordReset
from tailorcraft.domain.identity.errors import AccountMailQueueUnavailable, InvalidEmailAddress
from tailorcraft.domain.identity.value_objects import AddressedReset, EmailAddress
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    CountingClock,
    FakePasswordResetRepository,
    RecordingAccountMailQueue,
)
from tests.integration.identity.structure import constructor_dependency_names, module_references

_TTL = timedelta(minutes=60)


class _Rig:
    def __init__(self, clock: FixedClock, *, queue_error: Exception | None = None) -> None:
        self.log: list[str] = []
        self.resets = FakePasswordResetRepository(self.log)
        self.queue = RecordingAccountMailQueue(self.log, error=queue_error)
        self.clock = CountingClock(clock)
        self.use_case = RequestPasswordReset(self.resets, self.queue, self.clock, _TTL)


def test_the_constructor_is_given_neither_a_user_repository_nor_a_hasher() -> None:
    """AC-8, structural half one."""
    dependencies = constructor_dependency_names(RequestPasswordReset)
    assert "UserRepository" not in dependencies
    assert "PasswordHasherPort" not in dependencies


def test_the_module_never_names_a_user_repository_an_email_lookup_or_a_hasher() -> None:
    """AC-8, structural half two."""
    assert (
        module_references(
            RequestPasswordReset, {"UserRepository", "find_by_email", "PasswordHasherPort"}
        )
        == set()
    )


async def test_a_request_adds_one_addressed_reset_then_enqueues_its_id(clock: FixedClock) -> None:
    """AC-8 / V-41: the whole request path, and nothing else — no read, no hash."""
    rig = _Rig(clock)

    returned_id = await rig.use_case("Alex@Example.com")

    assert rig.log == ["resets.add", "queue.enqueue_password_reset"]
    assert len(rig.resets.added) == 1
    written = rig.resets.added[0]
    assert written.id == returned_id
    assert written.target == AddressedReset(EmailAddress.parse("alex@example.com"))
    assert written.requested_at == clock.now()
    assert written.expires_at == clock.now() + _TTL
    assert written.token_hash is None
    assert rig.queue.resets == [returned_id]
    assert rig.queue.registrations == []


async def test_the_call_sequence_is_identical_for_every_address(clock: FixedClock) -> None:
    """AC-8: an address that has an account and one that has none take the same two statements — the
    use case cannot see the difference, so two runs over different addresses log the same."""
    first = _Rig(clock)
    second = _Rig(clock)

    await first.use_case("has-an-account@example.com")
    await second.use_case("nobody@example.com")

    assert first.log == second.log == ["resets.add", "queue.enqueue_password_reset"]


async def test_one_clock_reading_per_request(clock: FixedClock) -> None:
    rig = _Rig(clock)

    await rig.use_case("alex@example.com")

    assert rig.clock.calls == 1


async def test_a_malformed_address_is_refused_before_any_write_or_enqueue(
    clock: FixedClock,
) -> None:
    """V-40 (422 at the route)."""
    rig = _Rig(clock)

    with pytest.raises(InvalidEmailAddress):
        await rig.use_case("not-an-address")

    assert rig.log == []


async def test_an_enqueue_failure_propagates_and_the_committed_row_stays(clock: FixedClock) -> None:
    rig = _Rig(clock, queue_error=AccountMailQueueUnavailable())

    with pytest.raises(AccountMailQueueUnavailable):
        await rig.use_case("alex@example.com")

    assert rig.log == ["resets.add", "queue.enqueue_password_reset"]
    assert len(rig.resets.all()) == 1
