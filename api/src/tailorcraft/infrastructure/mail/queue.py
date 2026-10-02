"""`CeleryAccountMailQueue` — the `AccountMailQueuePort` adapter (slice 2.5, technical plan §0.4,
§0.10, AC-23).

`tailoring/queue.py`'s shape for the fourth queue. The broker carries **one UUID string** per
message: no address, no token, no password hash. The worker re-reads the row by id, mints the token
there, and a row superseded in the meantime is simply not found (`MISSING`). This module is the only
place the two task names are written as strings; `tasks/identity_mail.py` imports them.

**Nothing here logs a broker error's message.** A kombu connection error stringifies to the broker
URL, which carries the Redis password, so the floor logs the exception's type and nothing else.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final
from uuid import UUID

import structlog
from celery import Celery

from tailorcraft.domain.identity.errors import AccountMailQueueUnavailable
from tailorcraft.domain.identity.value_objects import PasswordResetId, PendingRegistrationId

if TYPE_CHECKING:
    from tailorcraft.domain.identity.ports import AccountMailQueuePort

log = structlog.get_logger(__name__)

# The producer's and the consumer's one spelling of each name. A producer and a consumer that
# disagree produce `NotRegistered` on the worker, where nobody is reading, while the API answers 202.
DELIVER_REGISTRATION_MAIL_TASK_NAME: Final = "tailorcraft.identity.deliver_registration_mail"
DELIVER_PASSWORD_RESET_MAIL_TASK_NAME: Final = "tailorcraft.identity.deliver_password_reset_mail"  # noqa: S105 -- a task name, not a secret

# The `kind=` word of every line about a delivery, here and in the task's outcome line, so one log
# search follows a delivery from enqueue to outcome.
KIND_REGISTRATION: Final = "registration"
KIND_PASSWORD_RESET: Final = "password_reset"  # noqa: S105 -- a log word, not a secret

_EVENT_ENQUEUED: Final = "identity.mail_enqueued"
_EVENT_ENQUEUE_FAILED: Final = "identity.mail_enqueue_failed"


class CeleryAccountMailQueue:
    """Publish a pending registration's or a reset's id to the `mail` queue.

    Both dependencies are constructor arguments with no default, `CeleryTailoringQueue`'s reason:
    the queue the producer publishes to and the queue list the worker consumes are the two halves of
    a footgun (`tasks/app.py`), and a default here would be a third place the name is written. The
    composition root passes `settings.mail_queue_name` and the process's `Celery` app.
    """

    def __init__(self, celery_app: Celery, queue_name: str) -> None:
        self._app = celery_app
        self._queue_name = queue_name

    async def enqueue_registration(self, pending_id: PendingRegistrationId) -> None:
        """Raises `AccountMailQueueUnavailable` when the broker cannot take it (V-17)."""
        await self._publish(
            DELIVER_REGISTRATION_MAIL_TASK_NAME, KIND_REGISTRATION, pending_id.value
        )

    async def enqueue_password_reset(self, reset_id: PasswordResetId) -> None:
        """Raises `AccountMailQueueUnavailable` when the broker cannot take it (V-40's 503)."""
        await self._publish(
            DELIVER_PASSWORD_RESET_MAIL_TASK_NAME, KIND_PASSWORD_RESET, reset_id.value
        )

    async def _publish(self, task_name: str, kind: str, row_id: UUID) -> None:
        try:
            # `send_task` is synchronous socket I/O; awaited from an async route, a Redis that is
            # unreachable would freeze the loop for every user for kombu's connection timeout. One
            # thread hop is the fix (`CeleryTailoringQueue`'s comment, measured there).
            await asyncio.to_thread(
                self._app.send_task, task_name, args=[str(row_id)], queue=self._queue_name
            )
        except Exception as exc:
            # The floor: the ways a broker can refuse are not an allow-list anybody can complete
            # (`OperationalError`, a wrong password's `ResponseError`, `gaierror`, `TimeoutError`, …).
            # `Exception`, never `BaseException`, so a shutdown still cancels. `error_type` only:
            # the message is the broker URL with its password.
            log.warning(
                _EVENT_ENQUEUE_FAILED,
                kind=kind,
                id=str(row_id),
                queue=self._queue_name,
                error_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
            )
            # `from None`: the suppressed frame holds the broker URL.
            raise AccountMailQueueUnavailable from None

        log.info(_EVENT_ENQUEUED, kind=kind, id=str(row_id), queue=self._queue_name)


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance. Never executed.
    def _assert_implements_account_mail_queue(queue: CeleryAccountMailQueue) -> None:
        _: AccountMailQueuePort = queue
