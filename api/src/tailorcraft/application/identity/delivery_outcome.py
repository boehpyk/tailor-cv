"""`DeliveryOutcome`: what a delivery use case (`DeliverRegistrationMail`, `DeliverPasswordResetMail`)
hands back to the worker task that ran it (slice 2.5, technical plan §2, V-19 … V-31, V-42, V-43).

**Returned, never raised.** A mail that could not be sent is an expected outcome of a delivery, not
a fault in it: the row has already been decided on (kept, issued, or deleted), and the task's only
remaining job is the log line. So a `MailNotDelivered` from `AccountMailPort.send` ends here as a
`FAILED`/`RECIPIENT_REJECTED` value, and the task logs `outcome=`, `reason=` and `smtp_code=` from
it. **This layer does not log** (the house rule); the outcome is the channel.

**One shared type for both deliveries**, though `ACCOUNT_EXISTS_NOTICE_SENT` is registration's alone
and `NO_ACCOUNT` is reset's alone. Unlike two aggregates of the same shape, these are the *same*
fact — "what the worker did with one queued id" — reported by one task module in one log vocabulary;
two enums would differ only by the member each cannot return.

**Carries no address, no token and no id.** The task already holds the id it was given; everything
else here is a closed enum or a reply code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tailorcraft.domain.identity.errors import MailNotDelivered
from tailorcraft.domain.identity.value_objects import MailFailureReason
from tailorcraft.domain.shared.errors import InvariantViolated


class DeliveryStatus(StrEnum):
    """Which of the closed set of things happened to one queued delivery. The values are the log
    line's `outcome=` words (feature spec, *Delivery* table)."""

    SENT = "sent"
    """V-23 / V-43: a token was issued and committed, **then** the link was sent."""

    ACCOUNT_EXISTS_NOTICE_SENT = "account_exists_notice_sent"
    """V-22 (registration only): the address has a `User`; the pending row was deleted and committed
    **before** `AccountAlreadyExists` was sent. No token was minted."""

    SKIPPED = "skipped"
    """V-21 / V-30 / V-31: the row was already issued — a redelivery. Nothing sent, nothing written."""

    MISSING = "missing"
    """V-19: no row with this id (superseded, confirmed, used or swept). Nothing sent."""

    EXPIRED = "expired"
    """V-20: the row had expired before the worker reached it; it was deleted. Nothing sent."""

    NO_ACCOUNT = "no_account"
    """V-42 (reset only): no account has the address; the reset was deleted. **Nothing sent.**"""

    RECIPIENT_REJECTED = "recipient_rejected"
    """V-26: the provider refused the recipient. The row has no future and was deleted."""

    FAILED = "failed"
    """V-24 / V-25 / V-27 / V-28: the provider could not be reached, throttled us, or refused our
    credentials or sender — after the adapter's own retry. The row stays as it was (issued, with a
    link that was never delivered); the user's recovery is *Send it again*."""


# The statuses reached by a `send` the server answered with a refusal: the only ones that can carry
# its reply code. `SENT` is not here (V-23 amended): a success's `250` tells the log nothing.
_ANSWERED_BY_THE_SERVER = frozenset({DeliveryStatus.FAILED, DeliveryStatus.RECIPIENT_REJECTED})


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """One delivery's result: a `status`, and for a failed send the adapter's `reason` and SMTP
    reply `smtp_code` (a number, never the reply's text — `MailNotDelivered`'s rule).

    **Invariant** (`InvariantViolated` otherwise):

    - `reason` is set **iff** `status is FAILED` (`UNAVAILABLE`, `THROTTLED` or `PROVIDER_REFUSED`;
      never `RECIPIENT_REJECTED`, which is a status of its own because it changes what was written).
    - `smtp_code` is set only on `FAILED` or `RECIPIENT_REJECTED`, and only when the server answered
      with one (a timeout has no code).
    """

    status: DeliveryStatus
    reason: MailFailureReason | None = None
    smtp_code: int | None = None

    def __post_init__(self) -> None:
        if self.status is DeliveryStatus.FAILED:
            if self.reason is None:
                raise InvariantViolated("a failed delivery must carry its reason")
            if self.reason is MailFailureReason.RECIPIENT_REJECTED:
                # A rejected recipient changes what was written (the row is deleted), so it is a
                # status of its own and never folded into `FAILED`, which leaves the row as it was.
                raise InvariantViolated("a rejected recipient is a status, not a failure reason")
        elif self.reason is not None:
            raise InvariantViolated("only a failed delivery carries a reason")
        if self.smtp_code is not None and self.status not in _ANSWERED_BY_THE_SERVER:
            raise InvariantViolated("only a send the server refused carries an SMTP reply code")


def outcome_of_failed_send(failure: MailNotDelivered) -> DeliveryOutcome:
    """The outcome a `MailNotDelivered` from `AccountMailPort.send` becomes: `RECIPIENT_REJECTED` (a
    status, with the reply code) or `FAILED` carrying the reason and the code. What was *written*
    about the failure is the calling use case's decision, not this function's."""
    if failure.reason is MailFailureReason.RECIPIENT_REJECTED:
        return DeliveryOutcome(DeliveryStatus.RECIPIENT_REJECTED, smtp_code=failure.smtp_code)
    return DeliveryOutcome(
        DeliveryStatus.FAILED, reason=failure.reason, smtp_code=failure.smtp_code
    )
