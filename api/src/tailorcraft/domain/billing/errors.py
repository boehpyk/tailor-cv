"""Errors the `billing` bounded context raises.

An error class *is* its contract, so every one has a real body at the skeleton step. Ids and fixed
labels only; the API layer maps these to status codes.
"""

from __future__ import annotations

from tailorcraft.domain.billing.value_objects import ChargedRunRef
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.errors import DomainError


class NoCreditsLeft(DomainError):
    """A charge found a balance of 0 (CR-2)."""


class RunAlreadyCharged(DomainError):
    """A charge found an earlier charge for the same run (CR-3)."""

    def __init__(self, run: ChargedRunRef) -> None:
        super().__init__(f"run {run.value} is already charged")
        self.run = run


class RunAlreadyRefunded(DomainError):
    """A refund lost the race for `uq_billing_credit_entry_refund_run` to another refund of the same
    run, committed after this one loaded the account (F-14: the sweep against a redelivery).

    CR-4 cannot see it: each side loaded an account with no refund yet. The repository's `save`
    translates the unique-index violation into this, and `RecordRunFailure` re-raises it as the run's
    own conflict, because it is the same fact (the other side decided this run first) reached one
    write earlier."""

    def __init__(self, run: ChargedRunRef) -> None:
        super().__init__(f"run {run.value} is already refunded")
        self.run = run


class CreditAccountNotFound(DomainError):
    """The user has no credit account. Eager opening means this should never happen (AC-2)."""

    def __init__(self, user_id: UserId) -> None:
        super().__init__(f"user {user_id.value} has no credit account")
        self.user_id = user_id


class InvalidGrantAmount(DomainError):
    """A grant is not a whole number from 1 to 100."""
