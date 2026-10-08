"""API tests for the PDF layout on the export routes (slice 3.2, T17 RED; AC-21…AC-24, AC-26).

Both twins — the guest cookie routes (`/api/tailoring-runs/{id}/exports`, `/api/export-jobs/{id}`)
and the bearer routes (`/api/me/…`) — run every test through one `world` fixture, because the spec
says they share one handler body (AC-25) and a divergence between them is the defect to catch.

Written from `docs/specs/export-pdf-layout-templates/feature-spec.md`, not from the handler. Against
T16's skeleton the response always says `layout_template: null`, the handler drops the body's layout
and `LayoutTemplateNotApplicable` is unmapped (a 500), so each behavioural test goes red on a status
or value assertion. **Absence assertions ("no row", "not enqueued") are paired with a positive
control** — a valid POST in the same test that must add exactly one row — because the skeleton
satisfies every absence on its own.

`no-store` is asserted on the `/api/me/` responses the *handler* builds (the 202/200 and the
`layout_template_not_applicable` refusal). FastAPI's own body-validation 422 is built by the app, not
the handler; 3.1's amendment of AC-24 says that one is outside the rule, so it is not asserted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId, LayoutTemplate
from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.tailoring.value_objects import TailoredCv, TailoredDocumentKind
from tailorcraft.infrastructure.api.deps import get_export_queue
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    ME_EXPORT_JOBS,
    ME_RUNS,
    Entry,
    count_rows,
    error_body,
    error_code,
    export_bytes,
    mint_guest,
    new_client,
    register,
    seed_entry,
)
from tests.integration.fakes import FakeExportQueue

LAYOUTS = [layout.value for layout in LayoutTemplate]
_REVISED = "Revised curriculum vitae line for the layout tests. " * 20


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """The export limiter lives in Redis, which the rollback never reaches."""


@dataclass
class World:
    twin: str
    client: AsyncClient
    session: AsyncSession
    settings: Settings
    owner: Owner
    entry: Entry
    queue: FakeExportQueue
    headers: dict[str, str]
    at: datetime

    @property
    def exports_url(self) -> str:
        if self.twin == "guest":
            return f"/api/tailoring-runs/{self.entry.run_id.value}/exports"
        return f"{ME_RUNS}/{self.entry.run_id.value}/exports"

    def job_url(self, job_id: object) -> str:
        prefix = "/api/export-jobs" if self.twin == "guest" else ME_EXPORT_JOBS
        return f"{prefix}/{job_id}"

    async def post(self, **body: object) -> Response:
        return await self.client.post(self.exports_url, json=body, headers=self.headers)

    async def rows(self) -> int:
        return await count_rows(
            self.session, "export_job", tailoring_run_id=self.entry.run_id.value
        )

    async def seed_ready(
        self,
        layout: LayoutTemplate | None,
        *,
        document: TailoredDocumentKind = TailoredDocumentKind.CV,
        format: ExportFormat = ExportFormat.PDF,
    ) -> ExportJob:
        job = ExportJob.request(
            id=ExportJobId(value=uuid4()),
            owner=self.owner,
            tailoring_run_id=self.entry.run_id,
            document=document,
            format=format,
            layout_template=layout,
            run_version=self.entry.run_version,
            requested_at=self.at,
        )
        job.mark_started(self.at)
        job.mark_ready(byte_size=10, render_duration_ms=10, at=self.at)
        job.release_events()
        await SqlAlchemyExportJobRepository(self.session).add(job)
        await LocalFileStore(self.settings.upload_dir).put(job.storage_ref, export_bytes(job.id))
        await self.session.commit()
        return job

    async def edit_the_cv(self) -> None:
        repo = SqlAlchemyTailoringRunRepository(self.session)
        run = await repo.get(self.entry.run_id)
        run.revise_cv(TailoredCv(_REVISED), expected_version=run.version, at=self.at)
        await repo.save(run)
        await self.session.commit()


@pytest_asyncio.fixture(params=["guest", "account"])
async def world(
    request: pytest.FixtureRequest,
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
    session: AsyncSession,
    clock: FixedClock,
) -> World:
    queue = FakeExportQueue()
    app.dependency_overrides[get_export_queue] = lambda: queue
    at = clock.now()
    if request.param == "guest":
        owner: Owner = await mint_guest(client, session)
        headers: dict[str, str] = {}
    else:
        account = await register(client, settings)
        owner, headers = account.owner, account.headers
    entry = await seed_entry(session, settings, owner, at=at, ready_formats=())
    return World(request.param, client, session, settings, owner, entry, queue, headers, at)


def _no_store(world: World, response: Response) -> None:
    if world.twin == "account":
        assert response.headers.get("cache-control") == "no-store"


# --- AC-21: the layout is accepted and echoed ------------------------------------------------


@pytest.mark.parametrize("layout", LAYOUTS)
async def test_a_pdf_export_with_a_layout_is_202_and_carries_it(world: World, layout: str) -> None:
    response = await world.post(document="cv", format="pdf", layout_template=layout)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["layout_template"] == layout
    assert body["format"] == "pdf"
    assert response.headers["location"] == world.job_url(body["id"])
    _no_store(world, response)
    assert (
        await count_rows(world.session, "export_job", id=UUID(body["id"]), layout_template=layout)
        == 1
    ), "the layout is stored on the row"
    assert world.queue.enqueued == [ExportJobId(UUID(body["id"]))]


@pytest.mark.parametrize("body", [{}, {"layout_template": None}], ids=["omitted", "null"])
async def test_a_pdf_export_without_a_layout_is_classic(
    world: World, body: dict[str, object]
) -> None:
    response = await world.post(document="cover_letter", format="pdf", **body)

    assert response.status_code == 202, response.text
    assert response.json()["layout_template"] == "classic"
    _no_store(world, response)


async def test_a_docx_export_without_a_layout_is_202_with_a_null_layout(world: World) -> None:
    response = await world.post(document="cv", format="docx")

    assert response.status_code == 202, response.text
    assert response.json()["layout_template"] is None
    assert response.json()["format"] == "docx"


# --- AC-22: refusals, and no row -------------------------------------------------------------


@pytest.mark.parametrize("bad", ["QA_NO_SUCH_LAYOUT_7c1", "CLASSIC", "", "classic "])
async def test_an_unknown_layout_is_422_validation_error_without_echo_and_no_row(
    world: World, bad: str
) -> None:
    before = await world.rows()

    response = await world.post(document="cv", format="pdf", layout_template=bad)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"
    if bad.strip():
        assert bad not in response.text
    assert await world.rows() == before
    assert world.queue.enqueued == []
    # Positive control: the very same request with a valid layout adds exactly one row.
    ok = await world.post(document="cv", format="pdf", layout_template="modern")
    assert ok.status_code == 202, ok.text
    assert ok.json()["layout_template"] == "modern"
    assert await world.rows() == before + 1


@pytest.mark.parametrize("layout", LAYOUTS)
async def test_a_layout_on_a_docx_is_422_not_applicable_and_no_row(
    world: World, layout: str
) -> None:
    before = await world.rows()

    response = await world.post(document="cv", format="docx", layout_template=layout)

    assert response.status_code == 422, response.text
    assert error_code(response) == "layout_template_not_applicable"
    assert "message" in error_body(response)
    _no_store(world, response)
    assert await world.rows() == before
    assert world.queue.enqueued == []
    ok = await world.post(document="cv", format="docx")
    assert ok.status_code == 202, ok.text
    assert await world.rows() == before + 1


async def test_an_extra_key_is_still_422_validation_error_and_no_row(world: World) -> None:
    before = await world.rows()

    response = await world.post(document="cv", format="pdf", layout_template="modern", extra=1)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"
    assert await world.rows() == before
    ok = await world.post(document="cv", format="pdf", layout_template="modern")
    assert ok.status_code == 202, ok.text
    assert await world.rows() == before + 1


# --- AC-23: idempotency is per layout --------------------------------------------------------


@pytest.mark.parametrize("layout", LAYOUTS)
async def test_the_same_layout_twice_is_202_then_200_with_the_same_job(
    world: World, layout: str
) -> None:
    first = await world.post(document="cv", format="pdf", layout_template=layout)
    assert first.status_code == 202, first.text

    second = await world.post(document="cv", format="pdf", layout_template=layout)

    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["layout_template"] == layout
    assert await world.rows() == 1
    assert len(world.queue.enqueued) == 1


async def test_a_different_layout_at_the_same_version_is_a_new_202_job(world: World) -> None:
    ids = []
    for layout in LAYOUTS:
        response = await world.post(document="cv", format="pdf", layout_template=layout)
        assert response.status_code == 202, response.text
        assert response.json()["layout_template"] == layout
        ids.append(response.json()["id"])

    assert len(set(ids)) == len(LAYOUTS)
    assert await world.rows() == len(LAYOUTS)


async def test_omitting_the_layout_after_classic_is_the_same_job(world: World) -> None:
    first = await world.post(document="cv", format="pdf", layout_template="classic")
    assert first.status_code == 202, first.text

    second = await world.post(document="cv", format="pdf")

    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]


# --- AC-24: list and poll carry the layout; `current` is unaffected by it --------------------


async def test_list_and_poll_report_each_jobs_layout_and_null_for_docx(world: World) -> None:
    expected: dict[str, str | None] = {}
    for layout in LayoutTemplate:
        expected[str((await world.seed_ready(layout)).id.value)] = layout.value
    docx = await world.seed_ready(None, format=ExportFormat.DOCX)
    expected[str(docx.id.value)] = None

    listing = await world.client.get(world.exports_url, headers=world.headers)

    assert listing.status_code == 200, listing.text
    assert {item["id"]: item["layout_template"] for item in listing.json()["items"]} == expected
    for job_id, expected_layout in expected.items():
        poll = await world.client.get(world.job_url(job_id), headers=world.headers)
        assert poll.status_code == 200, poll.text
        assert poll.json()["layout_template"] == expected_layout
        assert poll.json()["current"] is True


async def test_after_an_edit_every_layouts_pdf_reads_not_current(world: World) -> None:
    jobs = [await world.seed_ready(layout) for layout in LayoutTemplate]

    await world.edit_the_cv()

    for job in jobs:
        poll = await world.client.get(world.job_url(job.id.value), headers=world.headers)
        assert poll.status_code == 200, poll.text
        assert poll.json()["current"] is False, poll.json()["layout_template"]


async def test_switching_layouts_changes_no_existing_jobs_current(world: World) -> None:
    classic = await world.seed_ready(LayoutTemplate.CLASSIC)
    modern = await world.seed_ready(LayoutTemplate.MODERN)

    switched = await world.post(document="cv", format="pdf", layout_template="formal")

    assert switched.status_code == 202, switched.text
    assert switched.json()["layout_template"] == "formal"
    for job in (classic, modern):
        poll = await world.client.get(world.job_url(job.id.value), headers=world.headers)
        assert poll.status_code == 200, poll.text
        assert poll.json()["current"] is True
        assert poll.json()["layout_template"] == ("classic" if job is classic else "modern")


# --- AC-26: the file download is unchanged by the layout -------------------------------------


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize(
    ("document", "filename"),
    [
        (TailoredDocumentKind.CV, "tailored-cv.pdf"),
        (TailoredDocumentKind.COVER_LETTER, "cover-letter.pdf"),
    ],
)
async def test_the_pdf_download_keeps_its_filename_and_headers_whatever_the_layout(
    world: World, layout: str, document: TailoredDocumentKind, filename: str
) -> None:
    job = await world.seed_ready(LayoutTemplate(layout), document=document)

    response = await world.client.get(world.job_url(job.id.value) + "/file", headers=world.headers)

    assert response.status_code == 200, response.text
    assert response.content == export_bytes(job.id)
    assert response.headers.get("content-type") == "application/pdf"
    assert response.headers.get("content-disposition") == f'attachment; filename="{filename}"'
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("x-content-type-options") == "nosniff"


async def test_a_non_classic_job_not_yet_rendered_is_409_export_not_ready(world: World) -> None:
    created = await world.post(document="cv", format="pdf", layout_template="modern")
    assert created.status_code == 202, created.text
    assert created.json()["layout_template"] == "modern"

    response = await world.client.get(
        world.job_url(created.json()["id"]) + "/file",
        headers=world.headers,
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "export_not_ready"


async def test_a_non_classic_ready_job_whose_file_is_gone_is_410(world: World) -> None:
    job = await world.seed_ready(LayoutTemplate.FORMAL)
    (world.settings.upload_dir / job.storage_ref.key).unlink()

    response = await world.client.get(world.job_url(job.id.value) + "/file", headers=world.headers)

    assert response.status_code == 410, response.text
    assert error_code(response) == "export_file_gone"
