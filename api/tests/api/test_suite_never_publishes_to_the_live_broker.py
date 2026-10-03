"""/verify r1 guard: a test that reaches a queue adapter through the app cannot publish for real.

The `published_tasks` fixture (conftest, autouse) replaces `tasks.app.app.send_task` — the object
every app-building fixture wires as `app.state.celery`. These tests prove the claim from both ends:
the singleton's `send_task` is the recorder, and a real API request that enqueues mail (with no
`get_account_mail_queue` override, the shape that used to publish to the dev worker) lands in the
recorder with the adapter's own call shape.

Mutation: remove the autouse fixture's `monkeypatch.setattr` -> both tests fail (the first on the
identity assertion, the second because the recorder is empty: the task went to the broker; measured, it reached the
dev worker).
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from httpx import AsyncClient

from tailorcraft.infrastructure.mail.queue import DELIVER_REGISTRATION_MAIL_TASK_NAME
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks.app import app as celery_app
from tests.conftest import PublishedTask


def test_the_process_singleton_does_not_publish_during_a_test(
    published_tasks: list[PublishedTask],
) -> None:
    assert celery_app.send_task.__name__ == "record"
    assert published_tasks == []


async def test_an_unmocked_register_request_publishes_only_to_the_recorder(
    app: FastAPI, client: AsyncClient, settings: Settings, published_tasks: list[PublishedTask]
) -> None:
    assert app.state.celery is celery_app

    response = await client.post(
        "/api/auth/register",
        json={
            "email": f"isolation-{uuid4().hex[:12]}@example.com",
            "password": "a very long real password",
        },
        headers={"Origin": settings.public_base_url},
    )

    assert response.status_code == 202
    assert [task.name for task in published_tasks] == [DELIVER_REGISTRATION_MAIL_TASK_NAME]
    assert published_tasks[0].options.get("ignore_result") is True
