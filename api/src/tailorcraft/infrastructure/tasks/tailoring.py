"""The tailoring task — a **thin entry point**, exactly like an HTTP route (ADR-0005).

Resolve the dependencies, call `ExecuteTailoringRun`, translate the outcome into one log line. There
is no business logic here and there must never be any: logic in a task is logic that can only be
exercised by running a worker, which means it is logic with no unit test and no failing assertion
when it breaks.

Three things about the shape of `run_tailoring` are load-bearing rather than incidental, and each
carries its reason at the line:

1. **It takes one UUID string and returns `None`** (AC-11, ADR-0014 §7) — both halves privacy
   controls, in opposite directions.
2. **It declares no retry** (AC-9, ADR-0014 §6) — one retry mechanism, in the adapter.
3. **It bridges sync Celery to async application code with `asyncio.run`**, which is what makes the
   engine's lifetime the composition root's problem (`tasks/container.py`).

**Nothing here logs a CV, a posting, a prompt or a completion.** The task never even sees one: the
only value it handles is a run id, and the only things it reports are that id, an outcome label and a
duration (Constitution §8).
"""

from __future__ import annotations

import asyncio
import time
from typing import Final
from uuid import UUID

import structlog

from tailorcraft.application.tailoring.execute_tailoring_run import (
    ExecuteTailoringRunCommand,
    ExecuteTailoringRunOutcome,
)
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.tailoring.queue import TAILORING_TASK_NAME
from tailorcraft.infrastructure.tasks.app import app
from tailorcraft.infrastructure.tasks.container import tailoring_use_case

log = structlog.get_logger(__name__)

# One event name per outcome, so "how many runs were abandoned yesterday" is a filter rather than a
# parse. The two named in the failure contract keep the contract's spelling exactly
# (feature-spec.md G-26, G-27); the other three follow the same shape.
#
# **`SKIPPED` maps to G-27's `tailoring.already_decided`, and that is slightly wider than the name
# suggests** — `ExecuteTailoringRun` returns `SKIPPED` both for a run that is already decided and for
# one that is `running` and not yet stale. The two are one outcome in the application layer, and
# giving them separate names here would mean inventing a distinction the task cannot actually make
# (it would have to re-read the row to find out, which is a second query to improve a log line).
# G-27's is the case the contract names and by far the common one — a redelivery finding a finished
# run — so it keeps the name, and the `outcome=skipped` field says what was actually returned.
_EVENT_BY_OUTCOME: Final[dict[ExecuteTailoringRunOutcome, str]] = {
    ExecuteTailoringRunOutcome.SUCCEEDED: "tailoring.run_succeeded",
    ExecuteTailoringRunOutcome.FAILED: "tailoring.run_failed",
    ExecuteTailoringRunOutcome.SKIPPED: "tailoring.already_decided",
    ExecuteTailoringRunOutcome.MISSING: "tailoring.run_missing",
    ExecuteTailoringRunOutcome.ABANDONED: "tailoring.run_abandoned",
}


# `celery` ships no type information (pyproject's mypy override says so), so `app.task` is `Any`
# and the decorator would silently make this function untyped — which is precisely the shape mypy
# is meant to catch. The ignore is narrow (`untyped-decorator` only) and the signature below is
# fully annotated, so the *body* is still strictly checked.
@app.task(name=TAILORING_TASK_NAME, bind=False, ignore_result=True)  # type: ignore[untyped-decorator]  # celery is untyped
def run_tailoring(tailoring_run_id: str) -> None:
    """Execute one queued tailoring run.

    **The argument is a UUID string and the return value is `None`, and both are privacy controls**
    (AC-11, ADR-0014 §7).

    Returning anything at all would park it in Redis for `result_expires = 3600` — one hour, outside
    Postgres, outside the 1.6 guest purge's reach, outside every retention promise this product
    makes, in a datastore with no backup policy and no column anyone audits. The thing this task
    produces is a tailored CV, so a return value here would be a second copy of a stranger's
    employment history somewhere nobody thinks of as a database. `ignore_result=True` makes that
    structural rather than a habit: even a future edit that returned something would have nowhere to
    put it.

    The argument is the mirror image. `sentry-sdk`'s Celery integration captures task arguments, so
    this signature *is* a log field set. A UUID is safe there; a CV would not be — which is why the
    task takes an id and reads everything else back from the row, and also why it is idempotent by
    construction (ADR-0014 §6): there is no second copy of the inputs travelling through Redis that
    could disagree with the database.

    **No retry is declared, and none may be added** (AC-9, ADR-0014 §6): no `autoretry_for`, no
    `retry_backoff`, no `max_retries`. A test asserts their absence, so "just add one for safety"
    turns a test red rather than passing quietly. Retrying needs to know *what kind* of failure this
    was — a 429 is worth a second attempt, a safety refusal is a refusal repeated at twice the price,
    an input that does not fit will not fit either — and only the adapter has that. By the time an
    exception reaches Celery it is opaque, and Celery's answer to an opaque failure is to run the
    whole task again. Two retry layers do not add, they multiply: 2 adapter attempts under Celery's
    default 3 retries is 8 paid calls for one button press, every one after the first re-entering a
    run the aggregate has already recorded as failed.

    The only exceptions that escape are infrastructure ones: Postgres unreachable while recording an
    outcome (G-28). Those escape on purpose, so Celery marks the task failed and Sentry sees it.
    **They are not redelivered.** A late-acked task that raises is still acked, because
    `task_acks_on_failure_or_timeout` defaults to True (`tasks/app.py`, at `task_acks_late`). The run
    stays `running`, and the stale-run sweep (`tasks/tailoring_sweep.py`) records it `failed` /
    `abandoned` once it is past `tailoring_stale_after_seconds` (G-25'). Every *business* failure is
    already a recorded state of the aggregate by the time this function sees it (ADR-0004), never an
    exception.
    """
    started_at = time.monotonic()

    # The sync/async bridge. A Celery worker process is synchronous and has no event loop anywhere,
    # so `asyncio.run` opens one, runs the coroutine and closes it — a **fresh loop per task**. That
    # is what forces the engine to be built inside the composition root rather than at module
    # import: an asyncpg connection is bound to the loop it was created on, so a module-level engine
    # would work for the first task in a process and raise `RuntimeError: got Future attached to a
    # different loop` on the second (`tasks/container.py`'s module docstring, and the same note in
    # `tests/conftest.py`).
    #
    # `UUID(...)` raising on a malformed argument is left to escape deliberately. The only publisher
    # is `CeleryTailoringQueue`, which formats a `TailoringRunId`, so a string that is not a UUID
    # means a message from something that should not be publishing here — a bug, and a bug belongs
    # in the error reporter, not in a branch that swallows it. The argument is a UUID string, which
    # is safe for Sentry to capture (ADR-0014 §7).
    outcome = asyncio.run(_execute(TailoringRunId(UUID(tailoring_run_id))))

    # The translation, and the entire body of work this entry point is allowed to do. One line, with
    # ids, a label and a duration — the outcome's own `StrEnum` value, so the field reads
    # `outcome=abandoned` rather than `ExecuteTailoringRunOutcome.ABANDONED`.
    log.info(
        _EVENT_BY_OUTCOME[outcome],
        tailoring_run_id=tailoring_run_id,
        outcome=outcome.value,
        duration_ms=int((time.monotonic() - started_at) * 1000),
    )


async def _execute(run_id: TailoringRunId) -> ExecuteTailoringRunOutcome:
    """Open the worker's unit of work, run the use case, close the transaction.

    The commit here is **not** the one that makes `running` visible mid-call — that one, and the one
    that records the outcome, happen inside the use case, one per write, through
    `CommittingTailoringRunRepository` (`tasks/container.py`). This closes whatever transaction the
    use case's last reads left open, and it is written at the boundary, inside the task's own error
    boundary, for the reason `deps.py` gives for the routers committing in the handler: a commit that
    fails in teardown fails somewhere nothing can report it properly.
    """
    async with tailoring_use_case() as (execute, session):
        outcome = await execute(ExecuteTailoringRunCommand(tailoring_run_id=run_id))
        await session.commit()
        return outcome
