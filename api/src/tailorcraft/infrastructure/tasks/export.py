"""The export task — a **thin entry point**, exactly like an HTTP route (ADR-0005).

Resolve the dependencies, call `RenderExportJob`, translate the outcome into one log line. There is
no business logic here and there must never be any: logic in a task is logic that can only be
exercised by running a worker, which means it is logic with no unit test and no failing assertion
when it breaks.

Three things about the shape of `render_export` are load-bearing rather than incidental, and each
carries its reason at the line — 1.3's three, written out again here rather than cross-referenced,
because a reader who opens this file to add a retry needs the argument in front of them:

1. **It takes one UUID string and returns `None`** (AC-18) — both halves privacy controls, in
   opposite directions.
2. **It declares no retry** (AC-18, ADR-0014 §6) — one retry mechanism, and it is not this one.
3. **It bridges sync Celery to async application code with `asyncio.run`**, which is what makes the
   engine's lifetime the composition root's problem (`tasks/container.py`).

**Nothing here handles a document.** The task never sees one: the only value it handles is a job id,
and the only things it reports are that id, an outcome label and a duration (Constitution §8). The
Markdown, the HTML and the rendered bytes all live and die inside the renderer's own frame, three
layers down.
"""

from __future__ import annotations

import asyncio
import time
from typing import Final
from uuid import UUID

import structlog

from tailorcraft.application.export.render_export_job import (
    RenderExportJobCommand,
    RenderExportJobOutcome,
)
from tailorcraft.domain.export.value_objects import ExportJobId
from tailorcraft.infrastructure.export.queue import EXPORT_TASK_NAME
from tailorcraft.infrastructure.tasks.app import app
from tailorcraft.infrastructure.tasks.container import export_use_case

log = structlog.get_logger(__name__)

# One event name per outcome, so "how many exports were abandoned yesterday" is a filter rather than
# a parse. The names are the failure contract's (X-33, X-34); the other three follow the same shape.
#
# **`SKIPPED` maps to X-34's `export.already_decided`, and that is slightly wider than the name
# suggests** — `RenderExportJob` returns `SKIPPED` for a job that is already decided, for one that is
# `rendering` inside the stale window, *and* for the loser of X-30's start race, where two deliveries
# of one job both read `queued` and the second `save` after `mark_started` raises
# `ExportJobConcurrentlyModified`. All three are one outcome in the application layer, and giving
# them separate names here would mean inventing a distinction the task cannot actually make (it
# would have to re-read the row to find out, which is a second query to improve a log line). X-34's
# is the case the contract names and by far the common one — a redelivery finding a finished job —
# so it keeps the name, and the `outcome=skipped` field says what was actually returned.
_EVENT_BY_OUTCOME: Final[dict[RenderExportJobOutcome, str]] = {
    RenderExportJobOutcome.READY: "export.job_ready",
    RenderExportJobOutcome.FAILED: "export.job_failed",
    RenderExportJobOutcome.SKIPPED: "export.already_decided",
    RenderExportJobOutcome.MISSING: "export.job_missing",
    RenderExportJobOutcome.ABANDONED: "export.job_abandoned",
}


# `celery` ships no type information (pyproject's mypy override says so), so `app.task` is `Any` and
# the decorator would silently make this function untyped — which is precisely the shape mypy is
# meant to catch. The ignore is narrow (`untyped-decorator` only) and the signature below is fully
# annotated, so the *body* is still strictly checked.
@app.task(name=EXPORT_TASK_NAME, bind=False, ignore_result=True)  # type: ignore[untyped-decorator]  # celery is untyped
def render_export(export_job_id: str) -> None:
    """Render one queued export job.

    **The argument is a UUID string and the return value is `None`, and both are privacy controls**
    (AC-18).

    Returning anything at all would park it in Redis for `result_expires = 3600` — one hour, outside
    Postgres, outside the 1.6 guest purge's reach, outside every retention promise this product
    makes, in a datastore with no backup policy and no column anyone audits. The thing this task
    produces is a rendered CV, so a return value here would be a second copy of a stranger's
    employment history somewhere nobody thinks of as a database — and this task's product is *bytes*,
    which makes the temptation ("just return the PDF") more concrete than it was for tailoring.
    `ignore_result=True` makes the refusal structural rather than a habit: even a future edit that
    returned something would have nowhere to put it. The file goes to `FileStorePort` and the row
    remembers its key; nothing travels back through the broker.

    The argument is the mirror image. `sentry-sdk`'s Celery integration captures task arguments, so
    this signature *is* a log field set. A UUID is safe there; a document would not be — which is why
    the task takes an id and reads everything else back from the row, and also why it is idempotent
    by construction (AC-18): there is no second copy of the inputs travelling through Redis that
    could disagree with the database. The same id is also the sole input to the file's key
    (`FileRef.for_export`), so even a delivery that raced past the start check would write the same
    bytes to the same key rather than leave a second orphan.

    **No retry is declared, and none may be added** (AC-18, ADR-0014 §6): no `autoretry_for`, no
    `retry_backoff`, no `max_retries`. A test asserts their absence, so "just add one for safety"
    turns a test red rather than passing quietly. Retrying needs to know *what kind* of failure this
    was — a timeout might be worth a second attempt, a document the parser cannot handle will not
    parse on the second pass either, and an output over the byte cap will be over it again — and only
    the adapter has that. By the time an exception reaches Celery it is opaque, and Celery's answer
    to an opaque failure is to run the whole task again. Two retry layers do not add, they multiply.
    The cost here is worker seconds rather than money, which makes the rule *easier* to break and no
    less wrong: a render loop is CPU that the tailoring queue on the same two slots pays for.

    The only exceptions that escape are infrastructure ones: Postgres unreachable while recording the
    outcome (X-32). Those escape on purpose, so Celery marks the task failed and Sentry sees it.
    **They are not redelivered.** A late-acked task that raises is still acked, because
    `task_acks_on_failure_or_timeout` defaults to True (`tasks/app.py`, at `task_acks_late`). The job
    stays `rendering`, and the stale-job sweep (`tasks/export_sweep.py`) records it `failed` /
    `abandoned` once it is past `export_stale_after_seconds` (X-29). Every *business* failure is
    already a recorded state of the aggregate by the time this function sees it (ADR-0004), never an
    exception — which is the whole of AC-19.
    """
    started_at = time.monotonic()

    # The sync/async bridge. A Celery worker process is synchronous and has no event loop anywhere,
    # so `asyncio.run` opens one, runs the coroutine and closes it — a **fresh loop per task**. That
    # is what forces the engine to be built inside the composition root rather than at module import:
    # an asyncpg connection is bound to the loop it was created on, so a module-level engine would
    # work for the first task in a process and raise `RuntimeError: got Future attached to a
    # different loop` on the second (`tasks/container.py`'s module docstring, and the same note in
    # `tests/conftest.py`).
    #
    # `UUID(...)` raising on a malformed argument is left to escape deliberately. The only publisher
    # is `CeleryExportQueue`, which formats an `ExportJobId`, so a string that is not a UUID means a
    # message from something that should not be publishing here — a bug, and a bug belongs in the
    # error reporter, not in a branch that swallows it. The argument is a UUID string, which is safe
    # for Sentry to capture.
    outcome = asyncio.run(_execute(ExportJobId(UUID(export_job_id))))

    # The translation, and the entire body of work this entry point is allowed to do. One line, with
    # an id, a label and a duration — the outcome's own `StrEnum` value, so the field reads
    # `outcome=abandoned` rather than `RenderExportJobOutcome.ABANDONED`. No byte size here: that
    # belongs to the adapter's `export.render_succeeded` and to the row, and duplicating it would
    # mean this line changed every time the renderer learned something new to report.
    log.info(
        _EVENT_BY_OUTCOME[outcome],
        export_job_id=export_job_id,
        outcome=outcome.value,
        duration_ms=int((time.monotonic() - started_at) * 1000),
    )


async def _execute(job_id: ExportJobId) -> RenderExportJobOutcome:
    """Open the worker's unit of work, run the use case, close the transaction.

    The commit here is **not** the one that makes `rendering` visible mid-render — that one, and the
    one that records the outcome, happen inside the use case, one per write, through
    `CommittingExportJobRepository` (`tasks/container.py`). This closes whatever transaction the use
    case's last reads left open, which on the `MISSING` path (a job whose session was purged, X-33)
    is the only transaction there was. It is written at the boundary, inside the task's own error
    boundary, for the reason `deps.py` gives for the routers committing in the handler: a commit that
    fails in teardown fails somewhere nothing can report it properly.
    """
    async with export_use_case() as (render, session):
        outcome = await render(RenderExportJobCommand(export_job_id=job_id))
        await session.commit()
        return outcome
