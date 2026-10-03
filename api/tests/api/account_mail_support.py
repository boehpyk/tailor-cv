"""Support for the slice-2.5 API tests: the mail queue and the worker half, in-process.

**Why a recording queue.** `get_account_mail_queue` publishes to the real `mail` queue on the broker
the dev stack's worker consumes. A test that registers an address would publish a task for a row its
own rolled-back transaction never committed — harmless, noisy, and a test that can pass on someone
else's worker. The API is handed a `RecordingAccountMailQueue` instead (the 1.3 precedent:
`app.dependency_overrides[get_tailoring_queue]`), and a failure on the broker is the same fake built
with an `error=`.

**Why the worker half runs here.** The test must learn the link's token **from the mail**, the way a
person does, never by reading a plaintext out of the database (there is none: only its hash is
stored, ADR-0027). `_build_registration_delivery` / `_build_password_reset_delivery` are the worker's
own composition root (`tasks/container.py`) — the same functions production calls, bound to *this
test's* session and a `RecordingAccountMailer` — exactly as `test_tailoring.py::_run_worker` calls
`container._build_use_case`. The clock is the production `SystemClock` on both halves (the API's
`get_clock` is not overridden by these tests), so a row is not "expired" by two clocks disagreeing.
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.account_mail import (
    AccountAlreadyExists,
    ConfirmYourEmail,
    ResetYourPassword,
)
from tailorcraft.domain.identity.value_objects import PasswordResetId, PendingRegistrationId
from tailorcraft.infrastructure.api.deps import get_account_mail_queue
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container
from tests.integration.fakes import RecordingAccountMailer, RecordingAccountMailQueue


def install_recording_queue(
    app: FastAPI, *, error: Exception | None = None
) -> RecordingAccountMailQueue:
    """Replace the app's `AccountMailQueuePort` with a recording fake; returns it."""
    queue = RecordingAccountMailQueue(error=error)
    app.dependency_overrides[get_account_mail_queue] = lambda: queue
    return queue


async def deliver_registration(
    settings: Settings, session: AsyncSession, pending_id: PendingRegistrationId
) -> RecordingAccountMailer:
    """Run `DeliverRegistrationMail` for `pending_id` the way the worker does; return the mailer."""
    mailer = RecordingAccountMailer()
    deliver = container._build_registration_delivery(settings, session, mailer)
    await deliver(pending_id)
    await session.commit()
    return mailer


async def deliver_password_reset(
    settings: Settings, session: AsyncSession, reset_id: PasswordResetId
) -> RecordingAccountMailer:
    """Run `DeliverPasswordResetMail` for `reset_id` the way the worker does; return the mailer."""
    mailer = RecordingAccountMailer()
    deliver = container._build_password_reset_delivery(settings, session, mailer)
    await deliver(reset_id)
    await session.commit()
    return mailer


def confirmation_token(mailer: RecordingAccountMailer) -> str:
    """The confirmation link's token, from the one mail the mailer recorded."""
    assert len(mailer.sent) == 1, mailer.sent
    mail = mailer.sent[0]
    assert isinstance(mail, ConfirmYourEmail), type(mail)
    return mail.token.reveal()


def reset_token(mailer: RecordingAccountMailer) -> str:
    """The reset link's token, from the one mail the mailer recorded."""
    assert len(mailer.sent) == 1, mailer.sent
    mail = mailer.sent[0]
    assert isinstance(mail, ResetYourPassword), type(mail)
    return mail.token.reveal()


def sent_account_exists_notice(mailer: RecordingAccountMailer) -> bool:
    return len(mailer.sent) == 1 and isinstance(mailer.sent[0], AccountAlreadyExists)
