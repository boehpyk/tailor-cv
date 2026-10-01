"""AC-34 — *a guest -> registered upgrade loses nothing* (slice 2.4, T25, **[proof]**): the Phase 2
gate clause "registration loses nothing", end to end over HTTP with a fake on every external port.

**The flow, in the spec's order.** As a guest (the 1.1 upload route mints the session): upload a CV,
paste a posting, fetch a second (fake `JobPostingFetcherPort`), tailor twice (the worker's own use
case with a fake `LlmPort`; one run **fails `llm_timeout`**), edit the first run's CV, export a PDF
and a DOCX (the worker's own `RenderExportJob`, a fake renderer, **real files** on a per-test
volume). The guest downloads both files and keeps their bytes. Register; claim. Then, over `/api/me/`
**only** (the bearer, never the cookie): the saved-CV list holds the CV; history lists both runs
newest first with the posting titles and the CV's filename (no "CV deleted"); re-open returns the
**edited** CV and its `version`; both export files download **byte-identical** to before the claim;
the failed run reads `failed` / `llm_timed_out` / `retryable: true`. Then a full purge with the clock
+30 days and an orphan sweep (files aged 60 h): every one of those holds again, through the same
function. A planted expired guest is the positive control that the purge really ran.

**Hard, not soft** (2.2's `/verify` lesson): every step asserts its own exact status; the fake LLM's
call count is asserted before anything about its arguments; nothing is gated on an earlier step.

**Mutation record (T25)** — each of the claim's four `UPDATE`s made to match no row in turn
(`SqlAlchemyGuestWorkClaim.transfer`), the named failure observed, the source restored byte-exact
(`git checkout -- api/src`; `git diff -- api/src` empty), then re-observed green. The first
assertion after the claim compares **per-table row counts** before and after, so every outcome names
its table (each run: 1 failed, at the same line, before any `/api/me/` read):
- `intake_base_cv`: `AssertionError: the claim did not carry every intake_base_cv row: guest had 1, the
  user owns 0`
- `posting_job_posting`: `... every posting_job_posting row: guest had 2, the user owns 0`
- `tailoring_run`: `... every tailoring_run row: guest had 2, the user owns 0`
- `export_job`: `... every export_job row: guest had 2, the user owns 0`
Unmutated: 1 passed. (The spec writes the failed run's reason as `llm_timeout`; 1.3's wire value, and
the one asserted here, is `llm_timed_out`.)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.application.tailoring.execute_tailoring_run import (
    ExecuteTailoringRunCommand,
    ExecuteTailoringRunOutcome,
)
from tailorcraft.domain.export.value_objects import ExportJobStatus
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.errors import LlmTimedOut
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.api.deps import get_job_posting_fetcher
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.retention import purge_command
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container as tailoring_container
from tests.api.claim_race_world import World
from tests.api.claim_retention_support import (
    age_on_disk,
    orphan_sweep,
    purge_plus_30_days,
)
from tests.api.me_support import (
    ME_BASE_CVS,
    ME_EXPORT_JOBS,
    ME_RUNS,
    bearer,
    new_client,
)
from tests.api.test_export import _run_export_worker
from tests.api.test_history_privacy_markers import _draft, _MarkerFetcher
from tests.integration.claim_race_support import (
    OWNED_TABLES,
    owned_row_counts,
    seed_guest_with_work,
    session_exists,
)
from tests.integration.fakes import FakeDocumentRenderer, FakeLlm

CLAIM_URL = "/api/me/guest-work/claim"
GUEST_POSTINGS = "/api/job-postings"
GUEST_RUNS = "/api/tailoring-runs"
PDF_BYTES = b"%PDF-1.7 the guest's exported curriculum vitae"
DOCX_BYTES = b"PK\x03\x04 the guest's exported curriculum vitae, as a docx"
EDITED_CV = (
    "An edited curriculum vitae line, written by the guest before registering. " * 8 + "End."
)


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """The register and claim limiters and the purge lock live in Redis, which no rollback reaches."""


@pytest_asyncio.fixture
async def world(
    settings: Settings, engine: AsyncEngine, password_hasher: object, tmp_path: Path
) -> AsyncIterator[World]:
    w = World(settings, engine, password_hasher, tmp_path)
    try:
        yield w
    finally:
        await w.cleanup()


@dataclass
class Upgrade:
    """What the guest did, as the values every later read is compared against."""

    cv_id: str
    pasted_posting_id: str
    fetched_posting_id: str
    fetched_title: str
    run_ok: str
    run_failed: str
    edited_version: int
    pdf_id: str
    docx_id: str


async def _run_worker(world: World, run_id: str, llm: FakeLlm) -> ExecuteTailoringRunOutcome:
    async with async_sessionmaker(world.engine, expire_on_commit=False)() as session:
        execute = tailoring_container._build_use_case(world.settings, session, llm)
        outcome = await execute(
            ExecuteTailoringRunCommand(tailoring_run_id=TailoringRunId(UUID(run_id)))
        )
        await session.commit()
    return outcome


async def _export(
    world: World, browser: AsyncClient, run_id: str, fmt: str, payload: bytes
) -> tuple[str, bytes]:
    """Request an export as the guest, render it with the worker's own use case onto the real
    volume, and download it as the guest. Returns the job id and the downloaded bytes."""
    requested = await browser.post(
        f"{GUEST_RUNS}/{run_id}/exports", json={"document": "cv", "format": fmt}
    )
    assert requested.status_code == 202, requested.text
    job_id = str(requested.json()["id"])
    async with async_sessionmaker(world.engine, expire_on_commit=False)() as session:
        await _run_export_worker(
            session,
            world.settings,
            job_id,
            renderer=FakeDocumentRenderer(payload),
            files=LocalFileStore(world.root),  # type: ignore[arg-type]
        )
    polled = await browser.get(f"/api/export-jobs/{job_id}")
    assert polled.status_code == 200, polled.text
    assert polled.json()["status"] == ExportJobStatus.READY.value
    downloaded = await browser.get(f"/api/export-jobs/{job_id}/file")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == payload
    return job_id, downloaded.content


async def _everything_is_reachable_as_the_user(
    browser: AsyncClient, headers: dict[str, str], up: Upgrade, *, when: str
) -> None:
    """The AC-34 reads, over `/api/me/` only. Called after the claim and again after the purge and the
    sweep: one function, so the two moments cannot be checked differently."""
    listed = await browser.get(ME_BASE_CVS, headers=headers)
    assert listed.status_code == 200, f"{when}: {listed.text}"
    assert [item["id"] for item in listed.json()["items"]] == [up.cv_id], (
        f"{when}: the saved-CV list does not hold exactly the guest's CV (table intake_base_cv)"
    )

    history = await browser.get(ME_RUNS, headers=headers)
    assert history.status_code == 200, f"{when}: {history.text}"
    items = history.json()["items"]
    assert [item["id"] for item in items] == [up.run_failed, up.run_ok], (
        f"{when}: history is not both runs, newest first (table tailoring_run)"
    )
    failed, ok = items
    for item in items:
        assert item["base_cv"] is not None, f"{when}: history reads 'CV deleted' (intake_base_cv)"
        assert item["base_cv"]["original_filename"] == "sample.txt", f"{when}: {item['base_cv']}"
        assert item["base_cv_id"] == up.cv_id
        assert item["posting"] is not None, f"{when}: a posting is missing (posting_job_posting)"
    assert failed["posting"]["title"] == up.fetched_title, f"{when}: {failed['posting']}"
    assert failed["posting"]["id"] == up.fetched_posting_id
    assert ok["posting"]["id"] == up.pasted_posting_id
    assert ok["posting"]["source"] == "pasted"
    assert (failed["status"], failed["failure_reason"], failed["retryable"]) == (
        "failed",
        "llm_timed_out",  # the spec says `llm_timeout`; 1.3's wire value is this
        True,
    ), f"{when}: {failed}"
    assert ok["status"] == "succeeded", f"{when}: {ok}"
    assert ok["edited"] is True, f"{when}: the edit is not marked"

    reopened = await browser.get(f"{ME_RUNS}/{up.run_ok}", headers=headers)
    assert reopened.status_code == 200, f"{when}: {reopened.text}"
    assert reopened.json()["tailored_cv"] == EDITED_CV, f"{when}: not the EDITED cv"
    assert reopened.json()["version"] == up.edited_version, f"{when}: {reopened.json()['version']}"
    assert reopened.json()["expires_at"] is None

    for job_id, payload in ((up.pdf_id, PDF_BYTES), (up.docx_id, DOCX_BYTES)):
        polled = await browser.get(f"{ME_EXPORT_JOBS}/{job_id}", headers=headers)
        assert polled.status_code == 200, f"{when}: {polled.text} (table export_job)"
        assert polled.json()["status"] == "ready", f"{when}: {polled.json()}"
        file = await browser.get(f"{ME_EXPORT_JOBS}/{job_id}/file", headers=headers)
        assert file.status_code == 200, f"{when}: {file.status_code} {file.text}"
        assert file.content == payload, f"{when}: the export file's bytes changed"


async def test_ac34_a_guest_who_registers_and_claims_loses_nothing_not_even_to_a_purge(
    world: World, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetched_title = "Staff Platform Engineer, Fully Remote"
    fetched_text = "Staff platform engineer, remote, fully async team. " * 6
    pasted_text = "We are hiring a platform engineer to build the deploy pipeline. " * 6
    fetcher = _MarkerFetcher(fetched_text, fetched_title)
    world.app.dependency_overrides[get_job_posting_fetcher] = lambda: fetcher

    account = await world.account()
    files = LocalFileStore(world.root)
    async with new_client(world.app) as browser:
        # --- the guest -------------------------------------------------------------------------
        guest = await world.mint(browser)  # uploads sample.txt: a real 1.1 upload, a real file
        guest_cvs = await browser.get("/api/base-cvs")
        assert guest_cvs.status_code == 200, guest_cvs.text
        (cv,) = guest_cvs.json()["items"]
        cv_id = str(cv["id"])

        pasted = await browser.post(GUEST_POSTINGS, json={"source": "pasted", "text": pasted_text})
        assert pasted.status_code == 201, pasted.text
        fetched = await browser.post(
            GUEST_POSTINGS, json={"source": "fetched", "url": "https://jobs.example.com/staff-1"}
        )
        assert fetched.status_code == 201, fetched.text
        assert fetcher.calls == 1

        async def request_run(posting_id: object) -> str:
            requested = await browser.post(
                GUEST_RUNS, json={"base_cv_id": cv_id, "job_posting_id": posting_id}
            )
            assert requested.status_code == 202, requested.text
            return str(requested.json()["id"])

        # One active run at a time: the first settles before the second is requested.
        run_ok = await request_run(pasted.json()["id"])
        ok_llm = FakeLlm(_draft("ac34-first-cv", "ac34-first-letter"))
        assert await _run_worker(world, run_ok, ok_llm) is ExecuteTailoringRunOutcome.SUCCEEDED
        run_failed = await request_run(fetched.json()["id"])
        failing_llm = FakeLlm(LlmTimedOut())
        assert (
            await _run_worker(world, run_failed, failing_llm) is ExecuteTailoringRunOutcome.FAILED
        )
        assert (len(ok_llm.calls), len(failing_llm.calls)) == (1, 1)

        current = await browser.get(f"{GUEST_RUNS}/{run_ok}")
        assert current.status_code == 200, current.text
        edited = await browser.put(
            f"{GUEST_RUNS}/{run_ok}/documents/cv",
            json={"content": EDITED_CV, "expected_version": current.json()["version"]},
        )
        assert edited.status_code == 200, edited.text
        edited_version = int(edited.json()["version"])

        pdf_id, guest_pdf = await _export(world, browser, run_ok, "pdf", PDF_BYTES)
        docx_id, guest_docx = await _export(world, browser, run_ok, "docx", DOCX_BYTES)
        assert (guest_pdf, guest_docx) == (PDF_BYTES, DOCX_BYTES)

        before = await owned_row_counts(world.engine, guest=guest.guest_session_id)
        assert before == {
            "intake_base_cv": 1,
            "posting_job_posting": 2,
            "tailoring_run": 2,
            "export_job": 2,
        }, before

        # --- register + claim ----------------------------------------------------------------------
        calls_before = (len(ok_llm.calls), len(failing_llm.calls))
        claimed = await browser.post(CLAIM_URL, headers=account.headers)
        assert claimed.status_code == 200, claimed.text
        assert (len(ok_llm.calls), len(failing_llm.calls)) == calls_before, (
            "the claim called the LLM"
        )
        assert not await session_exists(world.engine, guest.guest_session_id)
        after = await owned_row_counts(world.engine, user=account.user_id)
        for table in OWNED_TABLES:
            assert after[table] == before[table], (
                f"the claim did not carry every {table} row: guest had {before[table]}, "
                f"the user owns {after[table]}"
            )

        up = Upgrade(
            cv_id=cv_id,
            pasted_posting_id=str(pasted.json()["id"]),
            fetched_posting_id=str(fetched.json()["id"]),
            fetched_title=fetched_title,
            run_ok=run_ok,
            run_failed=run_failed,
            edited_version=edited_version,
            pdf_id=pdf_id,
            docx_id=docx_id,
        )
        # The user reads with the bearer alone: drop the guest cookie so nothing can fall back on it.
        browser.cookies.clear()
        headers = bearer(account.token)
        await _everything_is_reachable_as_the_user(browser, headers, up, when="after the claim")

        # --- a purge thirty days on, and the orphan sweep ------------------------------------------
        planted = await seed_guest_with_work(
            world.engine,
            files,
            started_at=datetime.now(UTC).replace(microsecond=0) - timedelta(hours=72),
        )
        world.guests.append(planted.guest.value)
        everything_on_disk = [
            FileRef(key=str(key))
            for (key,) in await _file_keys(world.engine, account.user_id.value)
        ]
        assert len(everything_on_disk) == 3, everything_on_disk  # the CV, the PDF and the DOCX
        age_on_disk(world.root, *everything_on_disk)

        assert await purge_plus_30_days(world.settings, monkeypatch) == purge_command.EXIT_OK
        assert not await session_exists(world.engine, planted.guest), "the purge never ran"
        assert not any((world.root / ref.key).exists() for ref in planted.files)
        assert await orphan_sweep(world.settings) == purge_command.EXIT_OK

        for ref in everything_on_disk:
            assert (world.root / ref.key).exists(), (
                f"a purge or sweep took a claimed file {ref.key}"
            )
        await _everything_is_reachable_as_the_user(
            browser, headers, up, when="after the purge and the sweep"
        )


async def _file_keys(engine: AsyncEngine, user: Any) -> list[tuple[str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT file_key FROM intake_base_cv WHERE user_id = :u "
                "UNION ALL SELECT file_key FROM export_job WHERE user_id = :u"
            ),
            {"u": user},
        )
        return [(str(r[0]),) for r in rows.all()]
