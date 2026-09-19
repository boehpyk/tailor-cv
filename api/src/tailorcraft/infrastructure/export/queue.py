"""`CeleryExportQueue` — the `ExportQueuePort` adapter (ADR-0005, ADR-0016 (d)).

The port says *make this render happen, not necessarily now*, in exactly those words and no others:
no task name, no broker, no routing key, no serialization (`domain/export/ports.py`). This module is
where that sentence becomes a Redis publish, and it is the only place in the codebase that knows the
export task's name string.

**This is `infrastructure/tailoring/queue.py` a second time, deliberately duplicated rather than
generalized**, and the duplication is the decision ADR-0014 §8 recorded and ADR-0016 (d) confirmed:
two one-method adapters with structurally identical bodies, and an obvious shared base class sitting
between them. Two is a coincidence; a supertype would freeze it into a rule guessing at what the
third queue wants. They also differ in what a refusal *costs* — a tailoring run that never calls the
model (money not spent) versus a render that never produces a file (worker seconds not spent) —
which is exactly the kind of difference a shared parent cannot hold, and which is why the two
exceptions they raise are two exceptions.

**Nothing here logs a document or a broker error message** (Constitution §8). A `kombu` connection
error stringifies to the **broker URL**, which carries the Redis password
(`redis://:pass@redis:6379/1`), so the floor logs the exception's fully-qualified *type* and nothing
else — exactly as the extractor, the Gemini adapter and the tailoring queue do.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final

import structlog
from celery import Celery

from tailorcraft.domain.export.errors import ExportNotQueued
from tailorcraft.domain.export.value_objects import ExportJobId

if TYPE_CHECKING:
    from tailorcraft.domain.export.ports import ExportQueuePort

log = structlog.get_logger(__name__)

# The one place the export task's name is written as a string. `tasks/export.py` imports it from
# here rather than repeating the literal: a producer and a consumer that disagree about a task name
# produce `NotRegistered` on the *worker*, which is to say in the log nobody is reading, while the
# API goes on answering 202 and the client polls a job that will never be picked up.
EXPORT_TASK_NAME: Final = "tailorcraft.export.render"

_EVENT_ENQUEUED: Final = "export.enqueued"
_EVENT_NOT_QUEUED: Final = "export.not_queued"


class CeleryExportQueue:
    """Publish an export job id to the named export queue.

    Both dependencies are constructor arguments with no defaults, and that is the point rather than
    ceremony: the queue name the producer publishes to and the queue list the worker consumes are
    the two halves of a footgun (see `tasks/app.py`), and a default here would be a third place the
    name could be written down. The composition roots pass `settings.export_queue_name` and the
    process's `Celery` app.

    Raises `ExportNotQueued`, never a `DocumentRenderFailed`, and the difference is the one
    `domain/export/ports.py` spells out: **nothing was rendered.** No worker second was spent, no
    bytes exist, the renderer was never reached — so this is not an outcome of a render and does not
    belong in the taxonomy of ways a render can fail. It is a plain `DomainError` with no `.reason`,
    and the router's answer to it is not "your export failed" but a 503 plus a job recorded `failed`
    / `not_queued` in a second transaction (X-22).
    """

    def __init__(self, celery_app: Celery, queue_name: str) -> None:
        self._app = celery_app
        self._queue_name = queue_name

    async def enqueue(self, job_id: ExportJobId) -> None:
        """Publish the job, or raise `ExportNotQueued`.

        Raises:
            ExportNotQueued: the broker refused the publish or is unreachable (X-22).
        """
        try:
            # `send_task` is **synchronous and does real socket I/O** — it opens (or borrows) a
            # connection to Redis and writes. This adapter is awaited from an async FastAPI route,
            # where a blocking call does not fail, it just stops the event loop for every concurrent
            # user (Constitution §6, and the 374 ms DOCX-sniff finding in CLAUDE.md). The happy path
            # is sub-millisecond; the path that matters is a Redis that is *unreachable*, where
            # kombu blocks for the connection timeout with the whole process frozen behind it. One
            # thread hop is the whole fix.
            #
            # `queue=` (rather than `exchange=`/`routing_key=`) publishes to the anonymous exchange
            # with the queue's name as the key, which reaches exactly one queue — see `tasks/app.py`
            # for why that distinction is load-bearing on a broker that remembers bindings.
            await asyncio.to_thread(
                self._app.send_task,
                EXPORT_TASK_NAME,
                args=[str(job_id.value)],
                queue=self._queue_name,
            )
        except Exception as exc:
            # THE CATCH-ALL FLOOR, load-bearing for the same reason the renderer's, the extractor's
            # and the tailoring queue's are. The port promises `ExportNotQueued` when the broker
            # refuses — and the set of ways a broker can refuse is not an allow-list anybody can
            # complete. `kombu.exceptions.OperationalError` is the documented one; underneath it sit
            # `redis.exceptions.ConnectionError`, `redis.exceptions.ResponseError` (a wrong
            # password), `socket.gaierror` (a hostname that no longer resolves), `TimeoutError`, and
            # whatever the next version of either library decides to raise. Naming them is a bet
            # that lost once already in this codebase, in the corruption sweep that found four
            # unlisted exception types escaping the CV extractor.
            #
            # `Exception`, never `BaseException`: a worker or a server shutting down must still be
            # able to cancel this (`asyncio.CancelledError` inherits from `BaseException`).
            #
            # `error_type` only — no `str(exc)`, no `exc_info`. A kombu connection error stringifies
            # to the **broker URL, password included**. That is the same class of hazard as a
            # `pypdf` message quoting document bytes, and it gets the same treatment.
            log.warning(
                _EVENT_NOT_QUEUED,
                export_job_id=str(job_id.value),
                queue=self._queue_name,
                error_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
            )
            # `from None`: the frame being suppressed holds the broker URL and the credential in it.
            raise ExportNotQueued(f"could not enqueue export job {job_id.value}") from None

        log.info(_EVENT_ENQUEUED, export_job_id=str(job_id.value), queue=self._queue_name)


if TYPE_CHECKING:
    # Makes mypy prove `CeleryExportQueue` structurally satisfies `ExportQueuePort` rather than
    # trusting the shape by eye — the same assertion every other adapter in this codebase carries,
    # and what `domain/export/ports.py` points at when it explains why a Protocol gets no red-first
    # cycle of its own.
    def _assert_implements_export_queue(queue: CeleryExportQueue) -> None:
        _: ExportQueuePort = queue
