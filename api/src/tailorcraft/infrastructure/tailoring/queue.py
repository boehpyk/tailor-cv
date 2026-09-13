"""`CeleryTailoringQueue` — the `TailoringQueuePort` adapter (ADR-0005, ADR-0014 §5).

The port says *make this run happen, not necessarily now*, in exactly those words and no others: no
task name, no broker, no routing key, no serialization (`domain/tailoring/ports.py`). This module is
where that sentence becomes a Redis publish, and it is the only place in the codebase that knows the
task's name string.

**Nothing here logs a document, a prompt or a broker error message** (Constitution §8). A `kombu`
connection error can carry the broker URL — which carries the Redis password — so the floor logs the
exception's fully-qualified *type* and nothing else, exactly as the extractor and the Gemini adapter
do.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

import structlog
from celery import Celery

from tailorcraft.domain.tailoring.errors import TailoringNotQueued
from tailorcraft.domain.tailoring.value_objects import TailoringRunId

if TYPE_CHECKING:
    from tailorcraft.domain.tailoring.ports import TailoringQueuePort

log = structlog.get_logger(__name__)

# The one place the task's name is written as a string. `tasks/tailoring.py` imports it from here
# rather than repeating the literal: a producer and a consumer that disagree about a task name
# produce `NotRegistered` on the *worker*, which is to say in the log nobody is reading, while the
# API goes on answering 202.
TAILORING_TASK_NAME: Final = "tailorcraft.tailoring.run"

_EVENT_ENQUEUED: Final = "tailoring.enqueued"
_EVENT_NOT_QUEUED: Final = "tailoring.not_queued"


class CeleryTailoringQueue:
    """Publish a run id to the named tailoring queue.

    Both dependencies are constructor arguments with no defaults, and that is the point rather than
    ceremony: the queue name the producer publishes to and the queue list the worker consumes are the
    two halves of a footgun (see `tasks/app.py`), and a default here would be a third place the name
    could be written down. The composition roots pass `settings.tailoring_queue_name` and the
    process's `Celery` app.

    Raises `TailoringNotQueued`, never a `TailoringFailed`, and the difference is ADR-0014 §2's line
    applied at the one moment it is hardest to see: **nothing was spent.** No call was made, no
    tokens were bought, no model was asked anything. `TailoringFailed` is the vocabulary of a call
    that happened; this is the vocabulary of a call that never will — which is why it is a plain
    `DomainError` with no `.reason`, and why the router's response to it is not "your run failed at
    the model" but a 503 plus a run recorded `failed` / `not_queued` in a second transaction (G-14).
    """

    def __init__(self, celery_app: Celery, queue_name: str) -> None:
        self._app = celery_app
        self._queue_name = queue_name

    async def enqueue(self, run_id: TailoringRunId) -> None:
        """Publish the run, or raise `TailoringNotQueued`.

        Raises:
            TailoringNotQueued: the broker refused the publish or is unreachable (G-14).
        """
        try:
            # `send_task` is **synchronous and does real socket I/O** — it opens (or borrows) a
            # connection to Redis and writes. This adapter is awaited from an async FastAPI route,
            # where a blocking call does not fail, it just stops the event loop for every concurrent
            # user (Constitution §6, and the 374 ms DOCX-sniff finding in CLAUDE.md). The happy path
            # is sub-millisecond; the path that matters is a Redis that is *unreachable*, where
            # kombu blocks for the connection timeout with the whole process frozen behind it. One
            # thread hop is the whole fix.
            await asyncio.to_thread(
                self._app.send_task,
                TAILORING_TASK_NAME,
                args=[str(run_id.value)],
                queue=self._queue_name,
            )
        except Exception as exc:
            # THE CATCH-ALL FLOOR, and it is load-bearing for the same reason the extractor's and
            # the Gemini adapter's are. The port promises `TailoringNotQueued` when the broker
            # refuses — and the set of ways a broker can refuse is not an allow-list anybody can
            # complete. `kombu.exceptions.OperationalError` is the documented one; underneath it
            # sit `redis.exceptions.ConnectionError`, `redis.exceptions.ResponseError` (a wrong
            # password), `socket.gaierror` (a hostname that no longer resolves), `TimeoutError`, and
            # whatever the next version of either library decides to raise. Naming them is a bet
            # that lost once already in this codebase, in the corruption sweep that found four
            # unlisted exception types escaping the extractor.
            #
            # `Exception`, never `BaseException`: a worker or a server shutting down must still be
            # able to cancel this (`asyncio.CancelledError` inherits from `BaseException`).
            #
            # `error_type` only — no `str(exc)`, no `exc_info`. A kombu connection error stringifies
            # to the **broker URL**, which contains the Redis password (`redis://:pass@redis:6379/1`).
            # That is the same class of hazard as a `pypdf` message quoting document bytes, and it
            # gets the same treatment.
            log.warning(
                _EVENT_NOT_QUEUED,
                tailoring_run_id=str(run_id.value),
                queue=self._queue_name,
                error_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
            )
            # `from None`: the frame being suppressed holds the broker URL and the credential in it.
            raise TailoringNotQueued(f"could not enqueue tailoring run {run_id.value}") from None

        log.info(_EVENT_ENQUEUED, tailoring_run_id=str(run_id.value), queue=self._queue_name)


if TYPE_CHECKING:
    # Makes mypy prove `CeleryTailoringQueue` structurally satisfies `TailoringQueuePort` rather
    # than trusting the shape by eye — the same assertion every other adapter in this codebase
    # carries, and what `domain/tailoring/ports.py` points at when it explains why a Protocol gets
    # no red-first cycle of its own.
    def _assert_implements_tailoring_queue(queue: CeleryTailoringQueue) -> None:
        _: TailoringQueuePort = queue
