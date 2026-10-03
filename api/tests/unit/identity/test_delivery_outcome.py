"""Unit tests for `DeliveryOutcome`'s invariant (slice 2.5, T13 RED — AC-6, AC-9).

Pure: no database, no loop. The rule (the skeleton docstring's, from the spec's delivery table):

- `reason` is set **iff** `status is FAILED`, and is never `RECIPIENT_REJECTED` (that is a status of
  its own, because it changes what was written);
- `smtp_code` is set only on `FAILED` or `RECIPIENT_REJECTED` (a timeout has none, so it stays
  optional there).

The skeleton's `__post_init__` is a no-op (AC-6), so every refusal below is red on
`Failed: DID NOT RAISE`, and every construction that must succeed is a positive control that passes
on arrival and pins the rule's other edge.
"""

from __future__ import annotations

import pytest

from tailorcraft.application.identity.delivery_outcome import DeliveryOutcome, DeliveryStatus
from tailorcraft.domain.identity.value_objects import MailFailureReason
from tailorcraft.domain.shared.errors import InvariantViolated

_NOT_FAILED = [s for s in DeliveryStatus if s is not DeliveryStatus.FAILED]
_NO_CODE_STATUSES = [
    s for s in DeliveryStatus if s not in (DeliveryStatus.FAILED, DeliveryStatus.RECIPIENT_REJECTED)
]
_FAILED_REASONS = [
    MailFailureReason.UNAVAILABLE,
    MailFailureReason.THROTTLED,
    MailFailureReason.PROVIDER_REFUSED,
]


def test_failed_without_a_reason_is_refused() -> None:
    with pytest.raises(InvariantViolated):
        DeliveryOutcome(DeliveryStatus.FAILED)


def test_failed_with_recipient_rejected_as_its_reason_is_refused() -> None:
    """`RECIPIENT_REJECTED` is a status, never a `FAILED` reason."""
    with pytest.raises(InvariantViolated):
        DeliveryOutcome(DeliveryStatus.FAILED, reason=MailFailureReason.RECIPIENT_REJECTED)


@pytest.mark.parametrize("status", _NOT_FAILED)
def test_a_reason_on_any_status_but_failed_is_refused(status: DeliveryStatus) -> None:
    with pytest.raises(InvariantViolated):
        DeliveryOutcome(status, reason=MailFailureReason.UNAVAILABLE)


@pytest.mark.parametrize("status", _NO_CODE_STATUSES)
def test_an_smtp_code_on_a_status_the_server_never_answered_is_refused(
    status: DeliveryStatus,
) -> None:
    with pytest.raises(InvariantViolated):
        DeliveryOutcome(status, smtp_code=250)


@pytest.mark.parametrize("reason", _FAILED_REASONS)
def test_failed_with_a_reason_and_no_code_is_accepted(reason: MailFailureReason) -> None:
    """A timeout has no reply code."""
    outcome = DeliveryOutcome(DeliveryStatus.FAILED, reason=reason)
    assert (outcome.status, outcome.reason, outcome.smtp_code) == (
        DeliveryStatus.FAILED,
        reason,
        None,
    )


def test_failed_with_a_reason_and_a_code_is_accepted() -> None:
    outcome = DeliveryOutcome(
        DeliveryStatus.FAILED, reason=MailFailureReason.THROTTLED, smtp_code=421
    )
    assert outcome.smtp_code == 421


def test_recipient_rejected_with_its_code_is_accepted() -> None:
    outcome = DeliveryOutcome(DeliveryStatus.RECIPIENT_REJECTED, smtp_code=550)
    assert (outcome.reason, outcome.smtp_code) == (None, 550)


@pytest.mark.parametrize("status", _NOT_FAILED)
def test_every_other_status_is_accepted_bare(status: DeliveryStatus) -> None:
    outcome = DeliveryOutcome(status)
    assert (outcome.status, outcome.reason, outcome.smtp_code) == (status, None, None)
