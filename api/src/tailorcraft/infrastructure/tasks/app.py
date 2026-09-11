"""The Celery application.

Tasks in this codebase are **thin entry points**. A task resolves its dependencies, calls an
application use case, and translates the outcome — exactly like an HTTP route, and it should be as
short as one. Business logic in a task is logic that can only be exercised by running a worker
(ADR-0005).

The beat schedule is empty in Phase 0. The guest-retention purge (FR-6) lands with slice 1.6, and
the roadmap deliberately keeps it *off* until it has been rehearsed by hand on real data — it issues
a `DELETE` against rows and unlinks files, and neither is reversible.

**The worker's observability is configured here, by Celery signal, and that is not decoration.**
`create_app`'s lifespan calls `configure_logging` / `configure_sentry` for the API process; nothing
called them for the worker, which means the worker — the process that actually holds a CV in memory
for twelve seconds — ran with stdlib logging defaults and, more to the point, **without
`_SILENCED_VENDOR_LOGGERS`**. That list exists because 1.2's `/verify` caught `httpx` logging full
URLs at INFO one frame below a clean adapter; the Gemini SDK is built on `httpx`, so the leak that
was closed in the API was wide open in the worker. See `_on_worker_start` below.
"""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.signals import celeryd_init, worker_process_init
from kombu import Queue

from tailorcraft.infrastructure.observability import configure_logging, configure_sentry
from tailorcraft.infrastructure.settings import get_settings

# The queue Celery publishes to when nothing says otherwise — Celery's own default name, written
# down rather than left implicit, because `task_queues` below turns the set of consumed queues into
# something explicit and a default that is not in the list is a task that vanishes.
DEFAULT_QUEUE_NAME = "celery"


def create_celery() -> Celery:
    """Build the Celery application from settings."""
    settings = get_settings()

    celery_app = Celery(
        "tailorcraft",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
        # Task modules to import at worker start. A task the worker never imported is not
        # registered, and a message for it is rejected as `NotRegistered` — a run that fails for a
        # reason that has nothing to do with tailoring. Strings, not imports: this module is
        # imported *by* `tasks/tailoring.py`, and Celery resolves these lazily at finalization.
        include=["tailorcraft.infrastructure.tasks.tailoring"],
    )
    celery_app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # Acknowledge AFTER the task completes, so a worker killed mid-render leaves the task on the
        # queue rather than losing it. Safe only because tasks here are required to be idempotent —
        # the pairing is the design, and dropping either half breaks the other.
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        # An export the user is waiting on must fail visibly rather than hang forever.
        task_soft_time_limit=120,
        task_time_limit=180,
        result_expires=3600,
        # --- The named queues (slice 1.3) ------------------------------------------------------
        #
        # Tailoring gets its **own** named queue from the first slice that needs one. One line now,
        # expensive to retrofit: 1.5's PDF and DOCX renders share this worker, and a long render
        # holding both of `--concurrency=2`'s slots would starve a tailoring run the user is
        # watching a spinner for. Separate queues are what let the two workloads be given separate
        # capacity later without touching either task.
        #
        # **The failure mode this list exists to prevent: a task published to `tailoring` while the
        # worker consumes only `celery` is a run that queues forever — and it looks exactly like a
        # healthy system.** The API answers, the row is committed `queued`, Redis is up, the worker
        # is up and reports itself idle, `/health/ready` is green, and nothing anywhere logs an
        # error. The only symptom is a spinner that never stops. That is why the queue list is
        # declared here, derived from the same `Settings` field the producer publishes with
        # (`CeleryTailoringQueue` reads `settings.tailoring_queue_name` too), rather than hard-coded
        # into the worker's command line in `docker-compose.yml` where it could drift out of step
        # with the producer in a one-word edit — and why the deploy verifies the running worker's
        # queue list rather than trusting either file (docs/cicd.md, and T36).
        #
        # A worker started with no `-Q` consumes exactly `task_queues`, so both entries below are
        # live. `-Q` on the command line still overrides this, which is what makes "give tailoring
        # its own worker" a compose change and not a code change.
        task_default_queue=DEFAULT_QUEUE_NAME,
        task_queues=(
            Queue(DEFAULT_QUEUE_NAME),
            Queue(settings.tailoring_queue_name),
        ),
        # Empty until slice 1.6. See the module docstring.
        beat_schedule={},
    )
    return celery_app


# Same narrow ignore as `tasks/tailoring.py`'s: `celery.signals` is untyped, so `.connect` would
# make this handler untyped. Its body stays strictly checked.
@celeryd_init.connect  # type: ignore[untyped-decorator]  # celery is untyped
def _on_worker_start(**_kwargs: Any) -> None:
    """Give the worker process the same logging and error-reporting configuration the API has.

    `celeryd_init` fires once in the main worker process, before the pool forks;
    `worker_process_init` fires in each forked child. Both are connected, because `structlog`'s
    configuration and the silenced vendor loggers are per-*process* state: with `--concurrency=2`
    and the default prefork pool, the children are where tasks actually run, and a child that
    inherited an unconfigured `logging` root is a child whose `httpx` records are not silenced.
    Configuring twice is idempotent and cheap; configuring once in the wrong process is a silent
    privacy regression.

    `configure_sentry` is a no-op without a DSN, so this costs nothing in dev and in CI.
    """
    settings = get_settings()
    configure_logging(settings)
    configure_sentry(settings)


# The same handler on the child-process signal — see `_on_worker_start`'s docstring for why both.
worker_process_init.connect(_on_worker_start)


app = create_celery()
