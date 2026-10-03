"""T41 regression: an enqueue must never subscribe the process to a task's result channel.

The wedge (production image, slice 2.5): `Celery.send_task` consults only its own `ignore_result`
kwarg. Without it, and with a Redis result backend configured, `send_task` calls
`backend.on_task_call`, which creates the result consumer's one shared `_pubsub` and SUBSCRIBEs to
`celery-task-meta-<id>`. The adapter discards the returned `AsyncResult`; its `__del__` then runs
`cancel_for` -> a synchronous `unsubscribe` on a non-thread-safe PubSub, on whichever thread drops
the last reference (the event loop's, after `to_thread`). An enqueue burst froze the whole API.
Nothing in the app reads a task result, so nothing should ever be subscribed.

The state asserted is the cause, not the symptom (a timing test cannot see the loop wedge, T47):
a real `Celery` app with a real Redis result backend on a test-only database, a real broker on
another test-only database, N enqueues through each adapter, then `_pubsub is None`,
`subscribed_to == set()` and no `celery-task-meta-*` channel seen from a second client.

Mutation: drop `ignore_result=True` from an adapter's `send_task` call -> `_pubsub` is non-None
after the first enqueue and its channel is listed by `PUBSUB CHANNELS`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
import redis
from celery import Celery

from tailorcraft.domain.export.value_objects import ExportJobId
from tailorcraft.domain.identity.value_objects import PasswordResetId, PendingRegistrationId
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.export.queue import CeleryExportQueue
from tailorcraft.infrastructure.mail.queue import CeleryAccountMailQueue
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tailoring.queue import CeleryTailoringQueue

# Databases this system does not use (0 cache, 1 broker, 2 results, 3 the suite's own).
_BACKEND_DB = 4
_BROKER_DB = 5
_ENQUEUES = 50


def _db_url(base: str, database: int) -> str:
    return urlunsplit(urlsplit(base)._replace(path=f"/{database}"))


def _identity(url: str) -> tuple[str | None, int | None, str]:
    parts = urlsplit(url)
    return (parts.hostname, parts.port or 6379, parts.path)


@pytest.fixture
def real_app(settings: Settings) -> Iterator[tuple[Celery, redis.Redis]]:
    backend_url = _db_url(settings.test_redis_url, _BACKEND_DB)
    broker_url = _db_url(settings.test_redis_url, _BROKER_DB)
    live = {_identity(u) for u in (settings.celery_broker_url, settings.celery_result_backend)} | {
        _identity(_db_url(settings.test_redis_url, 0))
    }
    assert _identity(backend_url) not in live, "the test backend collides with a live Redis role"
    assert _identity(broker_url) not in live, "the test broker collides with a live Redis role"

    app = Celery("qa-no-result-subscription", broker=broker_url, backend=backend_url)
    backend_client = redis.Redis.from_url(backend_url)
    broker_client = redis.Redis.from_url(broker_url)
    backend_client.flushdb()
    broker_client.flushdb()
    try:
        yield app, backend_client
    finally:
        backend_client.flushdb()
        broker_client.flushdb()
        backend_client.close()
        broker_client.close()
        app.close()


def _assert_never_subscribed(app: Celery, client: redis.Redis) -> None:
    consumer: Any = app.backend.result_consumer
    assert consumer._pubsub is None, "an enqueue opened the shared result pub/sub connection"
    assert consumer.subscribed_to == set()
    assert client.pubsub_channels("celery-task-meta-*") == []


async def test_tailoring_enqueues_never_subscribe_to_result_channels(
    real_app: tuple[Celery, redis.Redis],
) -> None:
    app, client = real_app
    queue = CeleryTailoringQueue(app, "tailoring")
    for _ in range(_ENQUEUES):
        await queue.enqueue(TailoringRunId(uuid4()))
    _assert_never_subscribed(app, client)


async def test_export_enqueues_never_subscribe_to_result_channels(
    real_app: tuple[Celery, redis.Redis],
) -> None:
    app, client = real_app
    queue = CeleryExportQueue(app, "export")
    for _ in range(_ENQUEUES):
        await queue.enqueue(ExportJobId(uuid4()))
    _assert_never_subscribed(app, client)


async def test_mail_enqueues_never_subscribe_to_result_channels(
    real_app: tuple[Celery, redis.Redis],
) -> None:
    app, client = real_app
    queue = CeleryAccountMailQueue(app, "mail")
    for _ in range(_ENQUEUES // 2):
        await queue.enqueue_registration(PendingRegistrationId(uuid4()))
        await queue.enqueue_password_reset(PasswordResetId(uuid4()))
    _assert_never_subscribed(app, client)
