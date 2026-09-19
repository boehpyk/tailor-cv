"""Task tests for `infrastructure/tasks/export.py::render_export` and
`infrastructure/tasks/export_sweep.py::abandon_stale_export_jobs` (I14t, test-after).

Mirrors `test_tailoring_task.py`'s and `test_tailoring_sweep_task.py`'s structure and their two
invocation techniques, for the identical sync/async-bridge reason documented there: both
`render_export` and `abandon_stale_export_jobs` are **synchronous** Celery tasks that bridge into
async application code with `asyncio.run(...)` (their own module docstrings, and
`infrastructure/tasks/container.py`'s):

1. **Where no visibility into this suite's own uncommitted data is needed** — inspecting the task
   objects' declared retry behaviour, and one real end-to-end call through each task's own
   `asyncio.run()` bridge for a case that touches no row this test wrote (an unknown job id; an
   empty sweep tick) — the tests below call `render_export(...)` / `abandon_stale_export_jobs()`
   **literally**, with `container.get_settings` patched to this suite's `settings` (so the fresh
   connection each opens lands on `tailorcraft_test`, never the dev database) and, where a renderer
   could otherwise run, `container.MarkdownDocumentRenderer` patched to a `FakeDocumentRenderer`
   that must never be called on that path. A plain `def test_...` (not `async def`) is required for
   this, exactly as `test_tailoring_task.py`'s own docstring explains: `asyncio.run()` refuses to
   start a second loop on top of pytest-asyncio's session-scoped one while an `async def` test's
   body is running.

2. **Where the task must see a job this test created** (AC-18's double-invocation) —
   `export.py`'s own `_execute` — the exact coroutine `render_export` hands to `asyncio.run()` — is
   called directly, `await`ed from an already-async test, with `export_use_case` (as imported into
   `infrastructure.tasks.export`) patched to yield the worker's real
   `_build_export_use_case(settings, session)` bound to *this test's own* session, and
   `container.MarkdownDocumentRenderer` patched to a `FakeDocumentRenderer` whose call count is
   what AC-18 is actually about.

Every export job built below needs a real, persisted, `succeeded` `TailoringRun` at the version the
job names: `RenderExportJob` step 5 reads the run back and compares `job.run_version` to
`run.version` (X-31), and a job pointing at a run version that does not match resolves as
`SOURCE_CHANGED` rather than exercising the render path this file means to test. Unlike
`TailoringRun`, `ExportJob` carries no foreign key to a `BaseCv` or a `JobPosting` — the run itself
is built with random ids for both, the same simplification `test_tailoring_sweep_task.py`'s own
module docstring makes for `AbandonStaleTailoringRuns`, one aggregate over.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.export.render_export_job import RenderExportJobOutcome
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.clock import FixedClock

# Importing these three mapping modules is what runs `mapper_registry.map_imperatively(...)` for
# `GuestSession`, `TailoringRun` and `ExportJob` as an import side effect — collection order alone
# cannot be trusted to do this first (unlike `make test`'s full run, this file may also be run alone
# via `make test file=...`, and then nothing else has imported them yet). Without it, each
# `repositories.*` module below fails at ITS OWN top level, per `test_tailoring_task.py`'s identical
# comment.
from tailorcraft.infrastructure.persistence.mapping.export.export_job import (
    export_job_table,  # noqa: F401
)
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,  # noqa: F401
)
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,  # noqa: F401
)
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container as export_container
from tailorcraft.infrastructure.tasks import export as export_task_module
from tailorcraft.infrastructure.tasks.export import render_export
from tailorcraft.infrastructure.tasks.export_sweep import abandon_stale_export_jobs
from tests.integration.export.support import a_documents, a_metrics
from tests.integration.fakes import FakeDocumentRenderer

# --- Test helpers ----------------------------------------------------------------------------------


async def _persist_owner(
    session: AsyncSession, clock: FixedClock, *, token_hash: str
) -> GuestSession:
    repo = SqlAlchemyGuestSessionRepository(session)
    owner = GuestSession.start(
        id=repo.next_identity(), token_hash=token_hash, at=clock.now(), ttl_hours=24
    )
    await repo.add(owner)
    return owner


async def _persist_succeeded_run(
    session: AsyncSession, owner_id: GuestSessionId, clock: FixedClock
) -> TailoringRun:
    """A real, persisted `succeeded` `TailoringRun` at `version == 3` (`request` -> `mark_started`
    -> `mark_succeeded`, each bumping `version` by one) — the only status `RenderExportJob` ever
    renders from. `base_cv_id`/`job_posting_id` are random: `ExportJob` carries no FK to either and
    `RenderExportJob` never looks them up (its own class docstring), so a real `BaseCv` or
    `JobPosting` row would prove nothing this file needs.
    """
    runs = SqlAlchemyTailoringRunRepository(session)
    run = TailoringRun.request(
        id=runs.next_identity(),
        guest_session_id=owner_id,
        base_cv_id=BaseCvId(value=uuid4()),
        job_posting_id=JobPostingId(value=uuid4()),
        requested_at=clock.now(),
    )
    run.mark_started(clock.now())
    run.mark_succeeded(a_documents(), a_metrics(), clock.now())
    await runs.add(run)
    return run


def _a_queued_export_job(
    jobs: SqlAlchemyExportJobRepository,
    owner_id: GuestSessionId,
    run: TailoringRun,
    clock: FixedClock,
    *,
    document: TailoredDocumentKind = TailoredDocumentKind.CV,
    format: ExportFormat = ExportFormat.PDF,
) -> ExportJob:
    return ExportJob.request(
        id=jobs.next_identity(),
        guest_session_id=owner_id,
        tailoring_run_id=run.id,
        document=document,
        format=format,
        run_version=run.version,
        requested_at=clock.now(),
    )


def _bind_export_task_to_this_sessions_worker(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    session: AsyncSession,
    fake_renderer: FakeDocumentRenderer,
) -> None:
    """The technique `test_tailoring_task.py`'s docstring calls approach 2, one layer over
    `_build_export_use_case`: `_execute` — the coroutine `render_export` hands to `asyncio.run()` —
    is left untouched; only the composition root it opens (`export_use_case`) is replaced with one
    bound to *this test's own* session, so a job this test just committed is visible to it without
    opening a second, genuinely separate connection. `container.MarkdownDocumentRenderer` is patched
    the same way `container.GeminiLlm` is patched in `test_tailoring_task.py` — it is imported at
    `container.py`'s module top level, so the name is a live seam.
    """
    monkeypatch.setattr(
        export_container, "MarkdownDocumentRenderer", lambda settings: fake_renderer
    )

    @asynccontextmanager
    async def _fake_export_use_case() -> AsyncIterator[tuple[Any, AsyncSession]]:
        yield export_container._build_export_use_case(settings, session), session

    monkeypatch.setattr(export_task_module, "export_use_case", _fake_export_use_case)


# --- AC-18: the task declares no Celery-level retry and ignores its result -------------------------


def test_the_task_declares_no_autoretry_for_retry_backoff_or_max_retries_and_ignores_result() -> (
    None
):
    """Inspection only — no execution needed, mirroring `test_tailoring_task.py`'s identical check
    on `run_tailoring`. Celery only ever installs `autoretry_for`/`retry_backoff` on a task when
    `autoretry_for` is passed to the decorator (`celery.app.task.Task._add_autoretry_behaviour`), so
    their plain *absence* from the task object is itself the proof nothing configured them.
    `max_retries` carries a class-level default (`3`) whether or not a task customises it, so the
    only way to prove THIS task did not override it is to compare against that same class default,
    never a bare literal. `ignore_result` is asserted directly: a return value here would park a
    rendered document's presence/absence in Redis for `result_expires`, outside every retention
    promise this product makes (`export.py`'s own module docstring).

    A test asserting all four is what turns "just add one for safety" into a red test rather than a
    silent, double-spending second retry layer or a Redis-parked result (AC-18).
    """
    from celery.app.task import Task

    assert not hasattr(render_export, "autoretry_for")
    assert not hasattr(render_export, "retry_backoff")
    assert render_export.max_retries == Task.max_retries
    assert render_export.ignore_result is True


# --- X-33: a job id that no longer exists is a missing return, not an exception ---------------------


def test_the_task_returns_none_and_logs_job_missing_for_an_unknown_job_id(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """`container.get_settings()` is the module-cached, process-wide settings — pointed at the DEV
    database by default. Patched here so the genuinely fresh connection this call opens lands on
    `tailorcraft_test`, never the dev database (this agent's own standing rule). No real renderer
    should ever run for a job that does not exist, so `MarkdownDocumentRenderer` is patched to a
    `FakeDocumentRenderer` whose call log this test asserts stays empty.
    """
    fake_renderer = FakeDocumentRenderer(b"unused")
    monkeypatch.setattr(export_container, "get_settings", lambda: settings)
    monkeypatch.setattr(
        export_container, "MarkdownDocumentRenderer", lambda settings: fake_renderer
    )

    with caplog.at_level(logging.INFO):
        result = render_export(str(uuid4()))

    assert result is None, "AC-18: the task must return None regardless of the outcome"
    assert fake_renderer.calls == [], "a missing job must never reach the renderer (X-33)"
    assert "export.job_missing" in caplog.text


# --- AC-18: two invocations for one queued job render exactly once ----------------------------------


async def test_two_invocations_for_one_queued_job_render_once(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="1" * 64)
    run = await _persist_succeeded_run(session, owner.id, clock)
    jobs = SqlAlchemyExportJobRepository(session)
    job = _a_queued_export_job(jobs, owner.id, run, clock)
    await jobs.add(job)
    await session.commit()

    fake_renderer = FakeDocumentRenderer(b"%PDF-1.7 fake bytes")
    _bind_export_task_to_this_sessions_worker(monkeypatch, settings, session, fake_renderer)

    first = await export_task_module._execute(job.id)
    assert first is RenderExportJobOutcome.READY
    assert len(fake_renderer.calls) == 1

    second = await export_task_module._execute(job.id)

    assert second is RenderExportJobOutcome.SKIPPED, (
        "a redelivery of an already-decided job must return SKIPPED, per export.job_ready -> "
        "export.already_decided (X-34)"
    )
    assert len(fake_renderer.calls) == 1, "AC-18: a second invocation must not render again"


# --- The sweep task: one log line, even on a tick that finds nothing to do --------------------------


def test_the_sweep_task_logs_one_line_on_an_empty_tick(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """A job that does nothing and logs nothing is indistinguishable from one that never ran
    (`export_sweep.py`'s own module docstring). This test seeds nothing: nothing else in this suite
    commits a real, un-rolled-back `rendering` `export_job` row (every other export test either uses
    fakes over an in-memory repository or this suite's own transactional `session` fixture, whose
    writes never survive past the test), so a call landing on `tailorcraft_test` through a genuinely
    separate connection finds an honestly empty batch — `swept_count`, `conflicts` and
    `examined_count` all zero — rather than merely "some count, logged".

    Same literal-call technique as `test_tailoring_sweep_task.py`'s own end-to-end test:
    `container.get_settings` patched so the fresh connection this call opens lands on
    `tailorcraft_test`, never the dev database.
    """
    monkeypatch.setattr(export_container, "get_settings", lambda: settings)

    with caplog.at_level(logging.INFO):
        result = abandon_stale_export_jobs()

    assert result is None, "ignore_result=True: the task must return None regardless of the outcome"
    assert "export.stale_jobs_swept" in caplog.text
    # structlog's renderer here is process-wide and picked once by whichever test configures it
    # first (`configure_logging`) — console-formatted (`swept_count=0`) if this file runs alone,
    # JSON (`"swept_count": 0`) inside the full suite. The regexes below match either rendering,
    # exactly as `test_tailoring_sweep_task.py`'s own log-line assertions do.
    assert re.search(r'"?swept_count"?\s*[:=]\s*0\b', caplog.text), caplog.text
    assert re.search(r'"?conflicts"?\s*[:=]\s*0\b', caplog.text), caplog.text
    assert re.search(r'"?examined_count"?\s*[:=]\s*0\b', caplog.text), caplog.text
    assert re.search(r'"?duration_ms"?\s*[:=]', caplog.text), caplog.text
