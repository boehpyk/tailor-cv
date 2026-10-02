"""AC-23: the broker carries ids only (slice 2.5, T23, test-after).

`CeleryAccountMailQueue` against a real `Celery` application whose `send_task` — the one call that
touches a broker — is replaced **below** the adapter, so the adapter's `except Exception` floor and
its `to_thread` hop stay in the path. Nothing here needs Redis.

Source of truth: AC-23 and technical plan §0.4/§0.10. Task names are written out literally (not
imported from the module under test), so a producer/consumer disagreement — `NotRegistered` on a
worker nobody watches while the API answers 202 — fails here by name.
"""

from __future__ import annotations

import logging
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from celery import Celery
from kombu.exceptions import OperationalError

from tailorcraft.domain.identity.errors import AccountMailQueueUnavailable
from tailorcraft.domain.identity.value_objects import PasswordResetId, PendingRegistrationId
from tailorcraft.infrastructure.mail.queue import CeleryAccountMailQueue
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.settings import Settings, get_settings

_BROKER_URL_WITH_PASSWORD: Final = "redis://:hunter2-broker-password@redis:6379/1"


class _RecordingApp(Celery):  # type: ignore[misc]  # celery is untyped (pyproject override)
    """A real `Celery` whose `send_task` records instead of publishing."""

    def __init__(self) -> None:
        super().__init__("qa-mail-queue")
        self.published: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.failure: Exception | None = None

    def send_task(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self.failure is not None:
            raise self.failure
        self.published.append((name, args, kwargs))
        return None


@pytest.fixture(autouse=True)
def _structlog_through_stdlib() -> None:
    configure_logging(Settings(app_env="test"))


def _queue() -> tuple[CeleryAccountMailQueue, _RecordingApp]:
    app = _RecordingApp()
    return CeleryAccountMailQueue(app, get_settings().mail_queue_name), app


def test_the_queue_name_setting_defaults_to_mail() -> None:
    assert Settings(app_env="test").mail_queue_name == "mail"


async def test_a_registration_publishes_one_uuid_string_to_the_mail_queue() -> None:
    queue, app = _queue()
    pending = PendingRegistrationId(uuid4())

    await queue.enqueue_registration(pending)

    ((name, positional, kwargs),) = app.published
    assert name == "tailorcraft.identity.deliver_registration_mail"
    assert positional == ()
    assert kwargs == {"args": [str(pending.value)], "queue": "mail"}
    assert UUID(kwargs["args"][0]) == pending.value


async def test_a_password_reset_publishes_one_uuid_string_to_the_mail_queue() -> None:
    queue, app = _queue()
    reset = PasswordResetId(uuid4())

    await queue.enqueue_password_reset(reset)

    ((name, positional, kwargs),) = app.published
    assert name == "tailorcraft.identity.deliver_password_reset_mail"
    assert positional == ()
    assert kwargs == {"args": [str(reset.value)], "queue": "mail"}


async def test_each_enqueue_publishes_exactly_one_message() -> None:
    queue, app = _queue()

    await queue.enqueue_registration(PendingRegistrationId(uuid4()))
    await queue.enqueue_password_reset(PasswordResetId(uuid4()))

    assert len(app.published) == 2


@pytest.mark.parametrize(
    "failure",
    [
        OperationalError("broker gone"),
        ConnectionRefusedError("refused"),
        TimeoutError("timed out"),
        OSError("unreachable"),
        RuntimeError("anything else a vendor can raise"),
    ],
    ids=["kombu", "refused", "timeout", "oserror", "floor"],
)
async def test_a_broker_failure_becomes_account_mail_queue_unavailable(failure: Exception) -> None:
    queue, app = _queue()
    app.failure = failure

    with pytest.raises(AccountMailQueueUnavailable) as raised:
        await queue.enqueue_registration(PendingRegistrationId(uuid4()))

    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


async def test_a_broker_failure_on_a_reset_is_translated_the_same_way() -> None:
    queue, app = _queue()
    app.failure = OperationalError("broker gone")

    with pytest.raises(AccountMailQueueUnavailable):
        await queue.enqueue_password_reset(PasswordResetId(uuid4()))


async def test_a_broker_failure_logs_the_event_and_error_type_but_never_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue, app = _queue()
    app.failure = OperationalError(f"cannot connect to {_BROKER_URL_WITH_PASSWORD}")
    reset = PasswordResetId(uuid4())

    with caplog.at_level(logging.INFO), pytest.raises(AccountMailQueueUnavailable):
        await queue.enqueue_password_reset(reset)

    text = caplog.text
    assert "identity.mail_enqueue_failed" in text
    assert "OperationalError" in text
    assert str(reset.value) in text
    assert "hunter2-broker-password" not in text
    assert "redis://" not in text
    assert all(record.exc_info is None for record in caplog.records)


async def test_an_enqueue_logs_the_id_and_queue_and_no_failure_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue, _ = _queue()
    pending = PendingRegistrationId(uuid4())

    with caplog.at_level(logging.INFO):
        await queue.enqueue_registration(pending)

    assert "identity.mail_enqueued" in caplog.text
    assert str(pending.value) in caplog.text
    assert "identity.mail_enqueue_failed" not in caplog.text
