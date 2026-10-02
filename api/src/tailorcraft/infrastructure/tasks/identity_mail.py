"""The two account-mail tasks — **thin entry points**, exactly like a route (ADR-0005, slice 2.5).

Resolve the dependencies, call `DeliverRegistrationMail` or `DeliverPasswordResetMail`, translate the
returned `DeliveryOutcome` into one log line. Which mail goes, minting the token, committing its hash
before sending, and what a refusal does to the row are the use cases' work, and none of it is here.

The shape is `tasks/tailoring.py`'s, for its reasons:

1. **One UUID string in, `None` out, `ignore_result=True`.** The broker carries an id and nothing else
   (plan §0.4, AC-23): no address, no token. Sentry's Celery integration captures task arguments, so
   the signature is a log field set, and a UUID is safe there.
2. **No retry, no `autoretry_for`, no `countdown`** (ADR-0014 §6). The mail adapter retries
   transient failures inside its own deadline; a Celery retry on top would multiply attempts, and on
   Redis a `countdown` message is restored every `visibility_timeout` until it runs. After the
   adapter gives up the outcome is `FAILED`, logged, and the user's recovery is *Send it again*.
3. **`asyncio.run`**, a fresh loop per task, so the engine is built inside the composition root.

**Idempotent, keyed on the row's id**: a redelivery finds the row issued (`SKIPPED`) or gone
(`MISSING`) and sends nothing (V-30, V-31). A failure to commit escapes on purpose (V-29): Celery
records it, and its message is withheld by the engine's `handle_error` listener.

**The line names nothing personal**: a kind, the row's id, an outcome word, a reason word, a reply
code and a duration. The task never sees an address or a token; the use case returns neither.
"""

from __future__ import annotations

import asyncio
import time
from typing import Final
from uuid import UUID

import structlog

from tailorcraft.application.identity.delivery_outcome import DeliveryOutcome, DeliveryStatus
from tailorcraft.domain.identity.value_objects import (
    MailFailureReason,
    PasswordResetId,
    PendingRegistrationId,
)
from tailorcraft.infrastructure.mail.queue import (
    DELIVER_PASSWORD_RESET_MAIL_TASK_NAME,
    DELIVER_REGISTRATION_MAIL_TASK_NAME,
    KIND_PASSWORD_RESET,
    KIND_REGISTRATION,
)
from tailorcraft.infrastructure.tasks.app import app
from tailorcraft.infrastructure.tasks.container import (
    deliver_password_reset_mail_use_case,
    deliver_registration_mail_use_case,
)

log = structlog.get_logger(__name__)

_EVENT_DELIVERED: Final = "identity.mail_delivered"
# V-27: our credentials or our sender refused. Every later mail will fail the same way until an
# operator fixes the configuration, so this one is at **error** level, where Sentry sees it.
_EVENT_PROVIDER_REFUSED: Final = "identity.mail_provider_refused"


# `celery` is untyped; the same narrow ignore as every other task. The signature is annotated, so
# the body is strictly checked.
@app.task(name=DELIVER_REGISTRATION_MAIL_TASK_NAME, bind=False, ignore_result=True)  # type: ignore[untyped-decorator]  # celery is untyped
def deliver_registration_mail(pending_registration_id: str) -> None:
    """Decide and send the mail for one pending registration: a confirmation link, or the
    account-exists notice (V-19 … V-31). A malformed id escapes as `ValueError`: the only publisher
    is `CeleryAccountMailQueue`, so anything else is a bug for the error reporter."""
    started_at = time.monotonic()
    outcome = asyncio.run(
        _deliver_registration(PendingRegistrationId(UUID(pending_registration_id)))
    )
    _log_outcome(KIND_REGISTRATION, pending_registration_id, outcome, started_at)


@app.task(name=DELIVER_PASSWORD_RESET_MAIL_TASK_NAME, bind=False, ignore_result=True)  # type: ignore[untyped-decorator]  # celery is untyped
def deliver_password_reset_mail(password_reset_id: str) -> None:
    """Decide and send the mail for one password reset: a reset link, or nothing at all when no
    account has the address (V-41 … V-44)."""
    started_at = time.monotonic()
    outcome = asyncio.run(_deliver_password_reset(PasswordResetId(UUID(password_reset_id))))
    _log_outcome(KIND_PASSWORD_RESET, password_reset_id, outcome, started_at)


async def _deliver_registration(pending_id: PendingRegistrationId) -> DeliveryOutcome:
    """Run the use case and close the transaction its last reads opened, inside the task's error
    boundary (`tasks/tailoring.py::_execute`'s reason)."""
    async with deliver_registration_mail_use_case() as (deliver, session):
        outcome = await deliver(pending_id)
        await session.commit()
        return outcome


async def _deliver_password_reset(reset_id: PasswordResetId) -> DeliveryOutcome:
    async with deliver_password_reset_mail_use_case() as (deliver, session):
        outcome = await deliver(reset_id)
        await session.commit()
        return outcome


def _log_outcome(kind: str, row_id: str, outcome: DeliveryOutcome, started_at: float) -> None:
    """The translation, and the only work this entry point does: one line per delivery. A send that
    may succeed later (`unavailable`, `throttled`) is a warning; our own credentials or sender
    refused is an error, under its own event name (V-27); everything else is the system working."""
    fields = {
        "kind": kind,
        "id": row_id,
        "outcome": outcome.status.value,
        "reason": outcome.reason.value if outcome.reason is not None else None,
        "smtp_code": outcome.smtp_code,
        "duration_ms": int((time.monotonic() - started_at) * 1000),
    }
    if outcome.reason is MailFailureReason.PROVIDER_REFUSED:
        log.error(_EVENT_PROVIDER_REFUSED, **fields)
    elif outcome.status is DeliveryStatus.FAILED:
        log.warning(_EVENT_DELIVERED, **fields)
    else:
        log.info(_EVENT_DELIVERED, **fields)
