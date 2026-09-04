"""The Celery application.

Tasks in this codebase are **thin entry points**. A task resolves its dependencies, calls an
application use case, and translates the outcome — exactly like an HTTP route, and it should be as
short as one. Business logic in a task is logic that can only be exercised by running a worker
(ADR-0005).

The beat schedule is empty in Phase 0. The guest-retention purge (FR-6) lands with slice 1.6, and
the roadmap deliberately keeps it *off* until it has been rehearsed by hand on real data — it issues
a `DELETE` against rows and unlinks files, and neither is reversible.
"""

from __future__ import annotations

from celery import Celery

from tailorcraft.infrastructure.settings import get_settings


def create_celery() -> Celery:
    """Build the Celery application from settings."""
    settings = get_settings()

    celery_app = Celery(
        "tailorcraft",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
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
        # Empty until slice 1.6. See the module docstring.
        beat_schedule={},
    )
    return celery_app


app = create_celery()
