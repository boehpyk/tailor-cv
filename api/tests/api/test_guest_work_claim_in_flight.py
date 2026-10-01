"""A claim that lands while the worker holds the work — slice 2.4, T23; AC-18 PROOF, AC-19, AC-20
(and C-5/C-6/C-27/C-28/C-29 of the failure contract; technical plan §0.7).

The three facts that let the claim move in-flight work without waiting for it (ADR-0025): the worker
never authorizes, it never writes an owner column, and **the claim never bumps `version`** — so the
worker's `UPDATE … WHERE version = :loaded` still matches after the claim commits. The third is
load-bearing, and AC-18 is its proof.

Every test drives the **real** worker use case (`tasks/container._build_use_case` /
`_build_export_use_case`, on its own pinned connection) with a fake at the external port, and the
claim through the **real route** (`POST /api/me/guest-work/claim` on `concurrent_app`, its own
session and connection). The claim is staged **inside the external call** — the moment the spec
names — and the test reads the row from a third connection at that instant to prove the work really
was `running` / `rendering` when it changed owner.

- **AC-18 (PROOF).** A guest run is `running`; the fake `LlmPort.tailor` commits a claim of its
  session over HTTP, then returns. The worker's outcome save succeeds: `succeeded`, **user-owned**,
  documents present, listed by `GET /api/me/tailoring-runs`. Mutation: the claim's run `UPDATE` also
  sets `version = version + 1`.
- **C-6 (queued variant).** The claim runs before the worker picks the run up; the worker executes it
  as the user's.
- **AC-19.** The same for a `rendering` export: `ready`, user-owned, its file downloads through
  `GET /api/me/export-jobs/{id}/file`.
- **AC-20.** A queued run built from a working copy (a 2.2-era row): the claim drops the copy, and
  the worker records `failed` / `base_cv_deleted` **before the paid call** — the fake LLM's call
  count is 0. Test-after: it composes 2.3's shipped branch with the claim and adds no code of its own.

**Mutation record (T23)** — applied to `src/` by hand, the named test watched going red, the source
restored byte-exact (`git diff --stat -- src` empty): see each docblock and the commit body.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.application.export.render_export_job import (
    RenderExportJobCommand,
    RenderExportJobOutcome,
)
from tailorcraft.application.tailoring.execute_tailoring_run import (
    ExecuteTailoringRunCommand,
    ExecuteTailoringRunOutcome,
)
from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import BaseCvLabel, ExtractedText
from tailorcraft.domain.posting.value_objects import JobPostingText
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.value_objects import (
    TailoredDocumentKind,
    TailoredDraft,
    TailoringRunId,
)
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container
from tests.api.claim_race_world import World
from tests.api.me_support import ME_EXPORT_JOBS, ME_RUNS, Account, new_client
from tests.integration.claim_race_support import pinned_session
from tests.integration.fakes import FakeLlm
from tests.integration.owners import (
    a_draft,
    extracted_cv,
    pasted_posting,
    queued_export,
    queued_run,
    succeeded_run,
)

CLAIM_URL = "/api/me/guest-work/claim"
_STEP_TIMEOUT = 30.0


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """The register and claim limiters live in Redis, which no rollback reaches."""


@pytest_asyncio.fixture
async def world(
    settings: Settings, engine: AsyncEngine, password_hasher: object, tmp_path: Path
) -> AsyncIterator[World]:
    w = World(settings, engine, password_hasher, tmp_path)
    try:
        yield w
    finally:
        await w.cleanup()


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)


_ROW_SQL = {
    "tailoring_run": (
        "SELECT status, user_id, guest_session_id, version, failure_reason, "
        "tailored_cv IS NOT NULL AS has_documents FROM tailoring_run WHERE id = :i"
    ),
    "export_job": (
        "SELECT status, user_id, guest_session_id, version, failure_reason "
        "FROM export_job WHERE id = :i"
    ),
}


async def _row(engine: AsyncEngine, table: str, row_id: UUID) -> dict[str, Any]:
    """One row from a third connection — what the rest of the system can see right now."""
    statement = _ROW_SQL[table]
    async with engine.connect() as conn:
        result = await conn.execute(text(statement), {"i": row_id})
        return dict(result.mappings().one())


async def _claim_over_http(browser: AsyncClient, account: Account) -> Response:
    return await browser.post(CLAIM_URL, headers=account.headers)


async def _seed_guest_run(
    world: World, guest: GuestOwner, *, base_cv_id: Any = None
) -> tuple[TailoringRunId, Any]:
    """A guest's CV, posting and **queued** run, committed. Returns the run id and the CV's id."""
    at = _now()
    async with async_sessionmaker(world.engine, expire_on_commit=False)() as session:
        cv = extracted_cv(guest, at)
        posting = pasted_posting(guest, at)
        await SqlAlchemyBaseCvRepository(session).add(cv)
        await SqlAlchemyJobPostingRepository(session).add(posting)
        run = queued_run(
            guest,
            at,
            base_cv_id=base_cv_id if base_cv_id is not None else cv.id,
            job_posting_id=posting.id,
        )
        await SqlAlchemyTailoringRunRepository(session).add(run)
        await session.commit()
        return run.id, cv.id


class _ClaimingLlm:
    """An `LlmPort` whose `tailor` commits the claim **inside the call** (the moment AC-18 names),
    after recording what a third connection sees of the run at that instant."""

    def __init__(
        self, world: World, browser: AsyncClient, account: Account, run_id: TailoringRunId
    ) -> None:
        self._world = world
        self._browser = browser
        self._account = account
        self._run_id = run_id
        self.calls = 0
        self.status_at_claim: str | None = None
        self.claim: Response | None = None

    async def tailor(self, cv: ExtractedText, posting: JobPostingText) -> TailoredDraft:
        self.calls += 1
        row = await _row(self._world.engine, "tailoring_run", self._run_id.value)
        self.status_at_claim = str(row["status"])
        self.claim = await _claim_over_http(self._browser, self._account)
        return a_draft()


# --- AC-18 (PROOF): a run claimed mid-call keeps its outcome ----------------------------------


async def test_ac18_a_run_claimed_during_the_llm_call_keeps_its_outcome_as_the_users(
    world: World,
) -> None:
    """AC-18 PROOF. At the instant of the claim the run is read from a **third connection** and is
    `running` (the worker committed that before the paid call); the claim answers 200 and counts the
    run; then the worker's outcome save — an `UPDATE … WHERE version = :loaded` — **still matches**:
    `SUCCEEDED`, the row is `succeeded`, user-owned, with its documents, and it is listed by
    `GET /api/me/tailoring-runs` (a user-owned run only answers there).

    **Mutation (T23), restored byte-exact:** the claim's `tailoring_run` `UPDATE` also sets
    `version = tailoring_run.version + 1` — the worker's outcome save loses. **Observed (1 failed, 3
    passed): the use case does not return `SKIPPED` here, it raises
    `TailoringRunConcurrentlyModified`** (the outcome save's conflict escapes by design, G-28; only
    step 4's conflict is `SKIPPED`). Either way a paid result is lost, which is the claim; the
    spec's word "`SKIPPED`" is imprecise and is reported with the T23 commit."""
    account = await world.account()
    async with new_client(world.app) as browser:
        guest = await world.mint(browser)
        run_id, _ = await _seed_guest_run(world, guest)
        llm = _ClaimingLlm(world, browser, account, run_id)

        async with pinned_session(world.engine) as worker_session:
            use_case = container._build_use_case(world.settings, worker_session, llm)
            outcome = await asyncio.wait_for(
                use_case(ExecuteTailoringRunCommand(tailoring_run_id=run_id)), _STEP_TIMEOUT
            )

        assert llm.calls == 1
        assert llm.status_at_claim == "running", "the claim must land while the run is `running`"
        assert llm.claim is not None
        assert llm.claim.status_code == 200, llm.claim.text
        assert llm.claim.json()["tailoring_runs"] == 1
        assert outcome is ExecuteTailoringRunOutcome.SUCCEEDED

        row = await _row(world.engine, "tailoring_run", run_id.value)
        assert row["status"] == "succeeded"
        assert row["user_id"] == account.user_id.value
        assert row["guest_session_id"] is None
        assert row["has_documents"] is True

        history = await browser.get(ME_RUNS, headers=account.headers)
        assert history.status_code == 200, history.text
        assert str(run_id.value) in {item["id"] for item in history.json()["items"]}


async def test_c6_a_queued_run_claimed_before_the_worker_picks_it_up_runs_as_the_users(
    world: World,
) -> None:
    """C-6 / AC-18's queued variant: the claim does not wait for a queued run (the worker may be
    down); when the worker comes back it executes the run, and the result is the user's."""
    account = await world.account()
    async with new_client(world.app) as browser:
        guest = await world.mint(browser)
        run_id, _ = await _seed_guest_run(world, guest)
        claim = await _claim_over_http(browser, account)
        assert claim.status_code == 200, claim.text
        assert claim.json()["tailoring_runs"] == 1
        assert (await _row(world.engine, "tailoring_run", run_id.value))["status"] == "queued"
        llm = FakeLlm(a_draft())

        async with pinned_session(world.engine) as worker_session:
            use_case = container._build_use_case(world.settings, worker_session, llm)
            outcome = await asyncio.wait_for(
                use_case(ExecuteTailoringRunCommand(tailoring_run_id=run_id)), _STEP_TIMEOUT
            )

        assert outcome is ExecuteTailoringRunOutcome.SUCCEEDED
        assert len(llm.calls) == 1
        row = await _row(world.engine, "tailoring_run", run_id.value)
        assert (row["status"], row["user_id"]) == ("succeeded", account.user_id.value)


# --- AC-19: an export claimed mid-render keeps its file ----------------------------------------


class _ClaimingRenderer:
    """A `DocumentRendererPort` that claims inside `render`, after recording the job's status."""

    BYTES = b"%PDF-1.7 rendered while the claim committed"

    def __init__(
        self, world: World, browser: AsyncClient, account: Account, job_id: ExportJobId
    ) -> None:
        self._world = world
        self._browser = browser
        self._account = account
        self._job_id = job_id
        self.calls = 0
        self.status_at_claim: str | None = None
        self.claim: Response | None = None

    async def render(
        self, markdown: str, *, document: TailoredDocumentKind, format: ExportFormat
    ) -> bytes:
        self.calls += 1
        row = await _row(self._world.engine, "export_job", self._job_id.value)
        self.status_at_claim = str(row["status"])
        self.claim = await _claim_over_http(self._browser, self._account)
        return self.BYTES


async def test_ac19_an_export_claimed_during_the_render_is_ready_and_the_users_with_its_file(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-19. The job is `rendering` (read from a third connection) when the claim commits; the
    render completes; the job is `ready`, user-owned, and its file downloads through
    `GET /api/me/export-jobs/{id}/file` — the bytes the worker wrote under the key the row names.

    **Mutation (T23), restored byte-exact:** the claim's `export_job` `UPDATE` also sets
    `version = export_job.version + 1` — red with `ExportJobConcurrentlyModified` (1 failed, 3 passed)."""
    account = await world.account()
    async with new_client(world.app) as browser:
        guest = await world.mint(browser)
        at = _now()
        async with async_sessionmaker(world.engine, expire_on_commit=False)() as session:
            cv = extracted_cv(guest, at)
            posting = pasted_posting(guest, at)
            await SqlAlchemyBaseCvRepository(session).add(cv)
            await SqlAlchemyJobPostingRepository(session).add(posting)
            run = succeeded_run(guest, at, base_cv_id=cv.id, job_posting_id=posting.id)
            await SqlAlchemyTailoringRunRepository(session).add(run)
            job = queued_export(guest, run, at, format=ExportFormat.PDF)
            await SqlAlchemyExportJobRepository(session).add(job)
            await session.commit()
        renderer = _ClaimingRenderer(world, browser, account, job.id)
        monkeypatch.setattr(container, "MarkdownDocumentRenderer", lambda settings: renderer)

        async with pinned_session(world.engine) as worker_session:
            use_case = container._build_export_use_case(world.settings, worker_session)
            outcome = await asyncio.wait_for(
                use_case(RenderExportJobCommand(export_job_id=job.id)), _STEP_TIMEOUT
            )

        assert renderer.calls == 1
        assert renderer.status_at_claim == "rendering", "the claim must land mid-render"
        assert renderer.claim is not None
        assert renderer.claim.status_code == 200, renderer.claim.text
        assert renderer.claim.json()["export_jobs"] == 1
        assert outcome is RenderExportJobOutcome.READY

        row = await _row(world.engine, "export_job", job.id.value)
        assert row["status"] == "ready"
        assert row["user_id"] == account.user_id.value
        assert row["guest_session_id"] is None

        download = await browser.get(
            f"{ME_EXPORT_JOBS}/{job.id.value}/file", headers=account.headers
        )
        assert download.status_code == 200, download.text
        assert download.content == _ClaimingRenderer.BYTES


# --- AC-20: a queued run made from a working copy ----------------------------------------------


async def test_ac20_a_queued_run_from_a_working_copy_fails_base_cv_deleted_before_the_paid_call(
    world: World,
) -> None:
    """AC-20. A hand-built 2.2-era row: a guest owns a **working copy** of the account's saved CV and
    a queued run over it. The claim drops the copy (and moves the run); the worker then records
    `failed` / `base_cv_deleted` **before** the paid call — the fake LLM's call count is 0 — on a run
    that is now the user's. 2.3's branch composed with the claim; no code of its own.

    **Mutation (T23), restored byte-exact:** the `copied_from_base_cv_id IS NULL` restriction removed
    from the claim's base-CV `UPDATE` (the copy is claimed, not dropped) — red on
    `assert 0 == 1` (`working_copies_dropped`), 1 failed, 3 passed.

    The paired positives (the claim counted one dropped copy and one run; the row is the user's) are
    what stop a claim that did nothing, or a worker that never ran, from satisfying `calls == []`."""
    account = await world.account()
    async with new_client(world.app) as browser:
        guest = await world.mint(browser)
        at = _now()
        async with async_sessionmaker(world.engine, expire_on_commit=False)() as session:
            cvs = SqlAlchemyBaseCvRepository(session)
            source = extracted_cv(UserOwner(account.user_id), at)
            source.rename(BaseCvLabel("Saved original"), at)
            source.release_events()
            await cvs.add(source)
            copy_id = cvs.next_identity()
            copy = BaseCv.copy_from(
                source=source,
                id=copy_id,
                into=guest,
                file=FileRef.for_base_cv(copy_id, source.content_type),
                at=at,
            )
            copy.release_events()
            await cvs.add(copy)
            await session.commit()
        run_id, _ = await _seed_guest_run(world, guest, base_cv_id=copy.id)

        claim = await _claim_over_http(browser, account)
        assert claim.status_code == 200, claim.text
        assert claim.json()["working_copies_dropped"] == 1
        assert claim.json()["tailoring_runs"] == 1
        llm = FakeLlm(a_draft())

        async with pinned_session(world.engine) as worker_session:
            use_case = container._build_use_case(world.settings, worker_session, llm)
            outcome = await asyncio.wait_for(
                use_case(ExecuteTailoringRunCommand(tailoring_run_id=run_id)), _STEP_TIMEOUT
            )

        assert outcome is ExecuteTailoringRunOutcome.FAILED
        assert llm.calls == [], "the paid call must not happen for a run whose CV is gone"
        row = await _row(world.engine, "tailoring_run", run_id.value)
        assert (row["status"], row["failure_reason"]) == ("failed", "base_cv_deleted")
        assert row["user_id"] == account.user_id.value
