"""Application tests for `RequestRegistration` (slice 2.5, T13 RED — AC-7, V-11, V-13 … V-18).

**The claim under test is an absence: the request path has no branch on the address.** An absence
is proven twice, because each proof alone has a hole:

- **Structurally** — the constructor is not given a `UserRepository`, and the module never names
  `UserRepository` or `find_by_email`. A recording double "with zero calls" cannot say this: the class
  is never handed it. (These two tests are green against the skeleton — an absence always is — and
  go red under the named mutation of AC-40: add a `find_by_email`.)
- **Behaviourally** — the positives that DO go red against the skeleton: exactly one hash, exactly
  one `put` of a freshly requested aggregate, exactly one enqueue of that aggregate's id, in that
  order, and the **identical call sequence** whether or not a pending registration already holds the
  address. The scenarios differ only in what the stores hold, which the use case cannot see.

All doubles share one call log (`tests/integration/fakes.py`), so "durable, then enqueued" is an
ordering assertion.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tailorcraft.application.identity.request_registration import RequestRegistration
from tailorcraft.domain.identity.errors import (
    AccountMailQueueUnavailable,
    InvalidEmailAddress,
    PasswordHashingFailed,
    WeakPassword,
)
from tailorcraft.domain.identity.pending_registration import PendingRegistration
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    PasswordHash,
    PasswordPolicy,
    WeakPasswordReason,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    CountingClock,
    FakePendingRegistrationRepository,
    LoggingPasswordHasher,
    RecordingAccountMailQueue,
)
from tests.integration.identity.structure import constructor_dependency_names, module_references

_TTL = timedelta(hours=24)
_NEW_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$bmV3aGFzaA")
_GOOD_PASSWORD = "correct horse battery staple"


class _Rig:
    def __init__(
        self,
        clock: FixedClock,
        *,
        hash_raises: Exception | None = None,
        queue_error: Exception | None = None,
    ) -> None:
        self.log: list[str] = []
        self.pending = FakePendingRegistrationRepository(self.log)
        self.hasher = LoggingPasswordHasher(
            self.log, hash_result=_NEW_HASH, hash_raises=hash_raises
        )
        self.queue = RecordingAccountMailQueue(self.log, error=queue_error)
        self.clock = CountingClock(clock)
        self.use_case = RequestRegistration(
            self.pending, self.hasher, self.queue, self.clock, PasswordPolicy(), _TTL
        )


def test_the_constructor_is_not_given_a_user_repository() -> None:
    """AC-7, structural half one: an absent port cannot be called."""
    assert "UserRepository" not in constructor_dependency_names(RequestRegistration)


def test_the_module_never_names_a_user_repository_or_an_email_lookup() -> None:
    """AC-7, structural half two: no import-inside-the-method, no module-level singleton."""
    assert module_references(RequestRegistration, {"UserRepository", "find_by_email"}) == set()


def test_the_structural_helper_would_see_the_violation_it_guards_against() -> None:
    """The proof above is only worth anything if its scanner can fail: feed it the mutation."""
    from tests.integration.identity.structure import referenced_names

    mutated = "async def f(self):\n    return await self._users.find_by_email(email)\n"
    assert {"find_by_email"} <= referenced_names(mutated)
    assert {"UserRepository"} <= referenced_names("from x import UserRepository as U\n")


async def test_a_request_hashes_once_puts_one_fresh_pending_registration_then_enqueues_its_id(
    clock: FixedClock,
) -> None:
    """AC-7 / V-14: the whole of the request path, in order, with the aggregate it wrote."""
    rig = _Rig(clock)

    returned_id = await rig.use_case("Alex@Example.com", _GOOD_PASSWORD)

    assert rig.log == ["hasher.hash", "pending.put", "queue.enqueue_registration"]
    assert len(rig.hasher.hash_calls) == 1
    assert len(rig.pending.put_calls) == 1
    written = rig.pending.put_calls[0]
    assert isinstance(written, PendingRegistration)
    assert written.id == returned_id
    assert written.email == EmailAddress.parse("alex@example.com")
    assert written.password_hash == _NEW_HASH
    assert written.requested_at == clock.now()
    assert written.expires_at == clock.now() + _TTL
    assert written.token_hash is None
    assert rig.queue.registrations == [returned_id]
    assert rig.queue.resets == []


async def test_the_call_sequence_is_identical_when_a_pending_registration_already_holds_the_address(
    clock: FixedClock,
) -> None:
    """AC-7 / V-16: nothing the requester could time differs between a new address and a repeat.
    The superseded row is replaced by `put` alone — no read first."""
    fresh = _Rig(clock)
    await fresh.use_case("alex@example.com", _GOOD_PASSWORD)

    repeat = _Rig(clock)
    earlier = PendingRegistration.request(
        repeat.pending.next_identity(),
        EmailAddress.parse("alex@example.com"),
        _NEW_HASH,
        clock.now(),
        _TTL,
    )
    repeat.pending.seed(earlier)
    new_id = await repeat.use_case("alex@example.com", _GOOD_PASSWORD)

    assert repeat.log == fresh.log == ["hasher.hash", "pending.put", "queue.enqueue_registration"]
    assert [p.id for p in repeat.pending.all()] == [new_id]


async def test_one_clock_reading_per_request(clock: FixedClock) -> None:
    rig = _Rig(clock)

    await rig.use_case("alex@example.com", _GOOD_PASSWORD)

    assert rig.clock.calls == 1


async def test_a_malformed_address_is_refused_before_any_hash_write_or_enqueue(
    clock: FixedClock,
) -> None:
    """V-11."""
    rig = _Rig(clock)

    with pytest.raises(InvalidEmailAddress):
        await rig.use_case("not-an-address", _GOOD_PASSWORD)

    assert rig.log == []


@pytest.mark.parametrize(
    ("password", "reason"),
    [
        ("short", WeakPasswordReason.TOO_SHORT),
        ("alex@example.com", WeakPasswordReason.MATCHES_EMAIL),
    ],
)
async def test_a_password_the_policy_refuses_is_refused_before_any_hash_write_or_enqueue(
    clock: FixedClock, password: str, reason: WeakPasswordReason
) -> None:
    """V-13: the policy runs before the ~50 ms hash is ever spent."""
    rig = _Rig(clock)

    with pytest.raises(WeakPassword) as refused:
        await rig.use_case("alex@example.com", password)

    assert refused.value.reason is reason
    assert rig.log == []


async def test_a_hashing_failure_propagates_with_nothing_written_or_enqueued(
    clock: FixedClock,
) -> None:
    """V-18."""
    rig = _Rig(clock, hash_raises=PasswordHashingFailed())

    with pytest.raises(PasswordHashingFailed):
        await rig.use_case("alex@example.com", _GOOD_PASSWORD)

    assert rig.log == ["hasher.hash"]
    assert rig.pending.all() == []
    assert rig.queue.registrations == []


async def test_an_enqueue_failure_propagates_and_the_committed_row_stays(
    clock: FixedClock,
) -> None:
    """V-17: the row was durable before the enqueue; *Send it again* supersedes it."""
    rig = _Rig(clock, queue_error=AccountMailQueueUnavailable())

    with pytest.raises(AccountMailQueueUnavailable):
        await rig.use_case("alex@example.com", _GOOD_PASSWORD)

    assert rig.log == ["hasher.hash", "pending.put", "queue.enqueue_registration"]
    assert len(rig.pending.all()) == 1
