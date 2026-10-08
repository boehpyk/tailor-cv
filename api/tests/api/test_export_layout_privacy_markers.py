"""AC-37 — the planted-marker test for the PDF layouts (slice 3.2, T18 RED).

A guest and an account each tailor-and-edit a run whose documents carry planted markers, then export
the CV as a PDF in **every** layout, one cover letter as a PDF, a DOCX, and both inline formats; each
file is rendered by the **real** `MarkdownDocumentRenderer` (so its own log lines exist) and
downloaded. Then, as a guest: register -> claim; and for both: delete the history entry -> erase the
account. **No marker** in any captured log record, domain event field, Sentry envelope or Redis key.

**Hard from the start** (2.2's lesson): every step asserts its exact status; nothing is gated on a
previous step. Step 3 deliberately does **not** assert the response's `layout_template` (T17 owns
that), so the flow runs to the end and the test goes red on its **positive control**:

  * the domain event lines for each explicitly chosen layout carry that `layout_template` **and the
    job id** (`ExportRequested`, via the tee and via the log), and
  * the renderer's own `export.render_started` / `export.render_succeeded` lines carry each layout
    (the renderer logs no job id, so the id travels on the event line, not the render line).

Positive controls come before the absence scans so that is where a red lands. Every absence assertion
is otherwise satisfiable by a flow that never ran, which is why they sit behind the controls.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sentry_sdk
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.export.render_export_job import (
    RenderExportJob,
    RenderExportJobCommand,
    RenderExportJobOutcome,
)
from tailorcraft.domain.export.events import ExportRequested
from tailorcraft.domain.export.value_objects import ExportJobId, LayoutTemplate
from tailorcraft.domain.identity.ownership import Owner, UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure import observability
from tailorcraft.infrastructure.api.deps import get_event_publisher, get_export_queue
from tailorcraft.infrastructure.clock import FixedClock, SystemClock
from tailorcraft.infrastructure.events.logging_publisher import LoggingEventPublisher
from tailorcraft.infrastructure.export.renderer import MarkdownDocumentRenderer
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks.container import CommittingExportJobRepository
from tests.api.me_support import (
    A_PASSWORD,
    DELETE_ACCOUNT_URL,
    ME_RUNS,
    bearer,
    mint_guest,
    new_client,
    seed_entry,
    seed_user_and_sign_in,
)
from tests.api.test_history_privacy_markers import (
    _CapturingTransport,
    _event_text,
    _TeePublisher,
)
from tests.integration.fakes import FakeExportQueue

CLAIM_URL: Final = "/api/me/guest-work/claim"
GUEST_RUNS: Final = "/api/tailoring-runs"
_PREFIX: Final = "QA37MARKER"
_LAYOUTS: Final = list(LayoutTemplate)


def _marker(label: str) -> str:
    return f"{_PREFIX}-{label}-{uuid4().hex}"


@pytest.fixture
def sentry_envelopes(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    transport = _CapturingTransport()
    real_init = sentry_sdk.init

    def _init(*args: Any, **kwargs: Any) -> Any:  # Any: sentry_sdk.init's own signature
        return real_init(*args, transport=transport, **kwargs)

    monkeypatch.setattr(sentry_sdk, "init", _init)
    observability.configure_sentry(
        settings.model_copy(update={"sentry_dsn": "https://public@sentry.example.invalid/1"})
    )
    try:
        yield transport.envelopes
    finally:
        real_init()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """Every step can touch a limiter; the rollback never reaches Redis."""


async def _render(session: AsyncSession, settings: Settings, job_id: str) -> None:
    """The worker's use case, in-process, with the **real** renderer and the real volume."""
    use_case = RenderExportJob(
        jobs=CommittingExportJobRepository(SqlAlchemyExportJobRepository(session), session),
        runs=SqlAlchemyTailoringRunRepository(session),
        renderer=MarkdownDocumentRenderer(settings),
        files=LocalFileStore(settings.upload_dir),
        events=LoggingEventPublisher(),
        clock=SystemClock(),
        stale_after_seconds=settings.export_stale_after_seconds,
    )
    outcome = await use_case(RenderExportJobCommand(export_job_id=ExportJobId(UUID(job_id))))
    assert outcome is RenderExportJobOutcome.READY, outcome


def _json_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for record in caplog.records:
        try:
            parsed = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(parsed, dict):
            lines.append(parsed)
    return lines


@pytest.mark.parametrize("twin", ["guest", "account"])
async def test_ac37_no_marker_leaks_across_every_layout_claim_deletion_and_erasure(
    twin: str,
    client: AsyncClient,
    app: FastAPI,
    session: AsyncSession,
    settings: Settings,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
    sentry_envelopes: list[str],
) -> None:
    email = f"{_marker('email').lower()}@example.com"
    cv_marker, letter_marker = _marker("edited-cv"), _marker("edited-letter")
    cv_edit = f"{cv_marker} " + "revised curriculum vitae line. " * 20
    letter_edit = f"{letter_marker} " + "dear hiring team, " * 20
    at = clock.now() - timedelta(minutes=10)

    publisher = _TeePublisher()
    app.dependency_overrides[get_event_publisher] = lambda: publisher
    app.dependency_overrides[get_export_queue] = lambda: FakeExportQueue()

    owner: Owner
    if twin == "guest":
        owner = await mint_guest(client, session)
        headers: dict[str, str] = {}
        runs_url, jobs_url = GUEST_RUNS, "/api/export-jobs"
    else:
        token, user_uuid = await seed_user_and_sign_in(client, settings, email=email)
        owner, headers = UserOwner(UserId(user_uuid)), bearer(token)
        runs_url, jobs_url = ME_RUNS, "/api/me/export-jobs"
    jobs_by_layout: dict[LayoutTemplate, str] = {}

    with caplog.at_level(logging.DEBUG):
        # One run per layout, so every POST is a *new* job: on a single run the skeleton's
        # layout-dropping handler would answer the second layout with 200 (the existing classic
        # job), and the test would go red on that status instead of on its positive control.
        async def tailored_run() -> str:
            entry = await seed_entry(session, settings, owner, at=at, ready_formats=())
            run_id = str(entry.run_id.value)
            edited_cv = await client.put(
                f"{runs_url}/{run_id}/documents/cv",
                json={"content": cv_edit, "expected_version": entry.run_version},
                headers=headers,
            )
            assert edited_cv.status_code == 200, edited_cv.text
            edited_letter = await client.put(
                f"{runs_url}/{run_id}/documents/cover_letter",
                json={"content": letter_edit, "expected_version": edited_cv.json()["version"]},
                headers=headers,
            )
            assert edited_letter.status_code == 200, edited_letter.text
            return run_id

        async def export(
            run_id: str, document: str, fmt: str, layout: LayoutTemplate | None
        ) -> str:
            body: dict[str, str] = {"document": document, "format": fmt}
            if layout is not None:
                body["layout_template"] = layout.value
            posted = await client.post(f"{runs_url}/{run_id}/exports", json=body, headers=headers)
            assert posted.status_code == 202, posted.text
            job_id = str(posted.json()["id"])
            await _render(session, settings, job_id)
            polled = await client.get(f"{jobs_url}/{job_id}", headers=headers)
            assert polled.status_code == 200, polled.text
            assert polled.json()["status"] == "ready", polled.text
            file = await client.get(f"{jobs_url}/{job_id}/file", headers=headers)
            assert file.status_code == 200, file.text
            assert len(file.content) > 0
            return job_id

        run_ids = [await tailored_run() for _ in _LAYOUTS]
        run_id = run_ids[0]
        for layout, rid in zip(_LAYOUTS, run_ids, strict=True):
            jobs_by_layout[layout] = await export(rid, "cv", "pdf", layout)
        await export(run_id, "cover_letter", "pdf", LayoutTemplate.FORMAL)
        await export(run_id, "cv", "docx", None)
        for fmt in ("md", "txt"):
            inline = await client.get(
                f"{runs_url}/{run_id}/documents/cv/download?format={fmt}", headers=headers
            )
            assert inline.status_code == 200, inline.text
            assert cv_marker in inline.text, "the inline download is the edited document"

        # --- claim (guest) -> the user's history -------------------------------------------------
        if twin == "guest":
            token, user_uuid = await seed_user_and_sign_in(client, settings, email=email)
            headers = bearer(token)
            claimed = await client.post(CLAIM_URL, headers=headers)
            assert claimed.status_code == 200, claimed.text
            runs_url = ME_RUNS
            opened = await client.get(f"{ME_RUNS}/{run_id}", headers=headers)
            assert opened.status_code == 200, opened.text
            owner = UserOwner(UserId(user_uuid))

        # --- delete the entry, then the account (with a second entry still holding a file) -----
        deleted = await client.delete(f"{runs_url}/{run_id}", headers=headers)
        assert deleted.status_code == 204, deleted.text
        await seed_entry(session, settings, owner, at=at)
        erased = await client.post(
            DELETE_ACCOUNT_URL,
            json={"password": A_PASSWORD},
            headers={**headers, "Origin": settings.public_base_url},
        )
        assert erased.status_code == 204, erased.text

    # --- Sentry: live channel ---------------------------------------------------------------------
    sentry_sdk.capture_message("QA37-sentry-probe")
    sentry_sdk.flush()
    sentry_text = "\n".join(sentry_envelopes)
    assert "QA37-sentry-probe" in sentry_text, "the Sentry capture is not live"

    # --- POSITIVE CONTROLS (first, so a red lands here) -------------------------------------------
    requested = {
        str(e.export_job_id.value): e for e in publisher.events if isinstance(e, ExportRequested)
    }
    lines = _json_lines(caplog)
    for layout, job_id in jobs_by_layout.items():
        assert requested[job_id].layout_template is layout, (
            f"the published ExportRequested for {job_id} carries "
            f"{requested[job_id].layout_template!r}, not the chosen {layout.value!r}"
        )
        assert any(
            line.get("event") == "domain_event"
            and line.get("event_type") == "ExportRequested"
            and line.get("export_job_id") == {"value": job_id}
            and line.get("layout_template") == layout.value
            for line in lines
        ), f"no domain_event log line names job {job_id} with layout_template={layout.value}"
        for render_line in ("export.render_started", "export.render_succeeded"):
            assert any(
                line.get("event") == render_line and line.get("layout_template") == layout.value
                for line in lines
            ), f"no {render_line} log line carries layout_template={layout.value}"

    # --- Absence ------------------------------------------------------------------------------------
    redis = create_redis(settings.redis_url)
    try:
        redis_keys = "\n".join(
            [key.decode() if isinstance(key, bytes) else key async for key in redis.scan_iter("*")]
        )
    finally:
        await redis.aclose()
    event_text = _event_text(publisher.events)
    assert publisher.events, "the API published no domain event — the tee is not wired"
    for description, marker in {
        "the email": email,
        "the edited CV": cv_marker,
        "the edited letter": letter_marker,
    }.items():
        assert marker not in caplog.text, f"{description} reached a log record"
        assert marker not in event_text, f"{description} reached a domain event"
        assert marker not in sentry_text, f"{description} reached a Sentry envelope"
        assert marker not in redis_keys, f"{description} reached a Redis key"
