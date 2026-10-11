"""Slice 4.3, T5 RED: the admin content router's HTTP contract (AC-3 ... AC-19).

Written from the spec (`docs/specs/admin-content/feature-spec.md`) against the T4 skeleton, whose
eleven handlers raise `NotImplementedError`. `new_client` uses `raise_app_exceptions=False`, so a
skeleton's failure is a real 500 and a red reads `assert 500 == 200`.

The app is a **concurrent** one (a real, committed session per request): the deletes' claims are
about what another connection sees, and every cross-request fact below is read on a fresh
`engine.connect()`. Accounts are real and deleted by id in teardown (the cascades take their rows);
seeds go through the real repositories and the domain's builders (`tests/integration/owners.py`), so
a seeded value is one the domain could have produced, and they **commit**.

AC-6/AC-7/AC-9 and the list reads are compared with the account's **own** `/api/me/` response on the
same data — the strongest proof that the admin route is the same use case from another seat.

Regression guards (green against the skeleton, by construction): the firewall rows of AC-16/AC-17
(the router-level `require_admin` runs before any handler), FastAPI's own 422s on malformed ids, the
Postgres-down 503s of AC-19 (the bearer check reads the database first) and AC-13's provider identity
(the skeleton already binds the same `Dep` aliases). Each is proven by mutation in T6.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session

from tailorcraft.domain.export.value_objects import ExportFailureReason, ExportFormat, ExportJobId
from tailorcraft.domain.identity.ownership import Owner, UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    BaseCvLabel,
    CvContentType,
    ExtractedText,
    OriginalFilename,
)
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import (
    FetchedPosting,
    JobPostingId,
    JobPostingText,
    PostingTitle,
    SourceUrl,
)
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.value_objects import CoverLetter, TailoredCv, TailoredDocuments
from tailorcraft.infrastructure.api.deps import (
    get_delete_saved_base_cv,
    get_erase_history_entry,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
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
from tests.api.me_support import (
    HISTORY_ENTRY_KEYS,
    JOB_POSTING_RESPONSE_KEYS,
    JOB_POSTING_SUMMARY_KEYS,
    Entry,
    assert_test_database,
    bearer,
    build_concurrent_app,
    error_body,
    error_code,
    export_bytes,
    mint_guest,
    new_client,
    override_settings,
    seed_entry,
    seed_user_and_sign_in,
)
from tests.api.test_auth_route_dependency_boundary import _all_dependency_calls, _iter_api_routes
from tests.integration.owners import (
    a_draft,
    extracted_cv,
    extraction_failed_cv,
    failed_run,
    pasted_posting,
    queued_export,
    queued_run,
    ready_export,
    running_run,
    uploaded_cv,
)

ADMIN_USERS = "/api/admin/users"
UNMATCHED = "/api/admin/no-such-route"
SAVED_CV_KEYS = {
    "id",
    "label",
    "original_filename",
    "content_type",
    "size_bytes",
    "status",
    "character_count",
    "failure_reason",
    "failure_message",
    "uploaded_at",
}
ERASURE_KEYS = {
    "tailoring_runs",
    "job_postings",
    "export_jobs",
    "tracked_applications",
    "files_unlinked",
    "unlink_failures",
}
ROUTE_NAMES = [
    "list_cvs",
    "get_cv",
    "delete_cv",
    "list_postings",
    "get_posting",
    "list_history",
    "get_run",
    "download",
    "list_exports",
    "delete_run",
    "export_file",
]
ITEM_ROUTES = [
    "get_cv",
    "delete_cv",
    "get_posting",
    "get_run",
    "download",
    "list_exports",
    "delete_run",
    "export_file",
]
ITEM_404 = {
    "get_cv": "base_cv_not_found",
    "delete_cv": "base_cv_not_found",
    "get_posting": "job_posting_not_found",
    "get_run": "tailoring_run_not_found",
    "download": "tailoring_run_not_found",
    "list_exports": "tailoring_run_not_found",
    "delete_run": "tailoring_run_not_found",
    "export_file": "export_job_not_found",
}
_PHC = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """Login limiters live in Redis, which the database rollback does not reach."""


# ----------------------------------------------------------------------------- fixtures


@pytest.fixture
def world_app(
    settings: Settings, engine: AsyncEngine, password_hasher: Argon2PasswordHasher
) -> FastAPI:
    assert_test_database(settings)
    return build_concurrent_app(settings, engine, password_hasher)


@pytest_asyncio.fixture
async def http(world_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(world_app) as client:
        yield client


@pytest_asyncio.fixture
async def seeded_ids(engine: AsyncEngine, settings: Settings) -> AsyncIterator[list[UUID]]:
    ids: list[UUID] = []
    yield ids
    assert_test_database(settings)
    async with engine.begin() as conn:
        for user_id in ids:
            await conn.execute(text("DELETE FROM identity_user WHERE id = :id"), {"id": user_id})
        for guest_id in _GUESTS:
            await conn.execute(
                text("DELETE FROM identity_guest_session WHERE id = :id"), {"id": guest_id}
            )
    _GUESTS.clear()


_GUESTS: list[UUID] = []


class _Who:
    """An account with a client of its own, so its `tc_refresh` cookie is not another's."""

    def __init__(self, client: AsyncClient, token: str, user_id: UUID) -> None:
        self.client = client
        self.token = token
        self.id = user_id

    @property
    def headers(self) -> dict[str, str]:
        return bearer(self.token)

    @property
    def owner(self) -> UserOwner:
        return UserOwner(UserId(self.id))


class MakeAccount(Protocol):
    async def __call__(self, *, admin: bool = False) -> _Who: ...


@pytest_asyncio.fixture
async def make_account(
    world_app: FastAPI, settings: Settings, engine: AsyncEngine, seeded_ids: list[UUID]
) -> AsyncIterator[MakeAccount]:
    clients: list[AsyncClient] = []

    async def make(*, admin: bool = False) -> _Who:
        client = new_client(world_app)
        clients.append(client)
        token, user_id = await seed_user_and_sign_in(client, settings)
        seeded_ids.append(user_id)
        if admin:
            async with engine.connect() as conn:
                await conn.execute(
                    text("UPDATE identity_user SET role = 'admin' WHERE id = :i"), {"i": user_id}
                )
                await conn.commit()
        return _Who(client, token, user_id)

    yield make
    for client in clients:
        await client.aclose()


def _at(clock: FixedClock, minutes: int) -> datetime:
    return clock.now() - timedelta(minutes=minutes)


async def _entry(
    world_app: FastAPI,
    settings: Settings,
    owner: Owner,
    at: datetime,
    **kwargs: Any,
) -> Entry:
    """A saved CV, a posting, a run and `ready` exports (files on disk), all committed."""
    async with world_app.state.session_factory() as session:
        return await seed_entry(session, settings, owner, at=at, **kwargs)


async def _add_cv(world_app: FastAPI, cv: Any, settings: Settings, *, with_file: bool) -> None:
    async with world_app.state.session_factory() as session:
        await SqlAlchemyBaseCvRepository(session).add(cv)
        await session.commit()
    if with_file:
        await LocalFileStore(settings.upload_dir).put(cv.file, b"original upload bytes")


async def _add_posting(world_app: FastAPI, posting: Any) -> None:
    async with world_app.state.session_factory() as session:
        await SqlAlchemyJobPostingRepository(session).add(posting)
        await session.commit()


async def _count(engine: AsyncEngine, sql: str, **params: object) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text(sql), params)).scalar_one())


async def _rows(engine: AsyncEngine, table: str, row_id: UUID) -> int:
    return await _count(engine, f"SELECT count(*) FROM {table} WHERE id = :i", i=row_id)  # noqa: S608


def _no_store(response: Response) -> None:
    assert response.headers.get("cache-control") == "no-store", dict(response.headers)


def _comparable(response: Response) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k != "date"}


def _routes(
    uid: object, cv: object, posting: object, run: object, job: object
) -> dict[str, tuple[str, str]]:
    """The eleven routes as (method, url), by name."""
    p = f"{ADMIN_USERS}/{uid}"
    return {
        "list_cvs": ("GET", f"{p}/base-cvs"),
        "get_cv": ("GET", f"{p}/base-cvs/{cv}"),
        "delete_cv": ("DELETE", f"{p}/base-cvs/{cv}"),
        "list_postings": ("GET", f"{p}/job-postings"),
        "get_posting": ("GET", f"{p}/job-postings/{posting}"),
        "list_history": ("GET", f"{p}/tailoring-runs"),
        "get_run": ("GET", f"{p}/tailoring-runs/{run}"),
        "download": ("GET", f"{p}/tailoring-runs/{run}/documents/cv/download?format=md"),
        "list_exports": ("GET", f"{p}/tailoring-runs/{run}/exports"),
        "delete_run": ("DELETE", f"{p}/tailoring-runs/{run}"),
        "export_file": ("GET", f"{p}/export-jobs/{job}/file"),
    }


def _random_routes(uid: object) -> dict[str, tuple[str, str]]:
    return _routes(uid, uuid4(), uuid4(), uuid4(), uuid4())


def _entry_routes(uid: object, entry: Entry) -> dict[str, tuple[str, str]]:
    return _routes(
        uid, entry.cv_id.value, entry.posting_id.value, entry.run_id.value, entry.jobs[0].id.value
    )


# ----------------------------------------------------------------------------- AC-3 CV list


async def test_ac3_the_cv_list_has_exactly_the_saved_cv_keys_no_text_newest_first_and_no_store(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    older = extracted_cv(target.owner, _at(clock, 30))
    newer = extracted_cv(target.owner, _at(clock, 10))
    await _add_cv(world_app, older, settings, with_file=False)
    await _add_cv(world_app, newer, settings, with_file=False)

    response = await http.get(f"{ADMIN_USERS}/{target.id}/base-cvs", headers=admin.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    items = response.json()["items"]
    assert set(response.json()) == {"items"}
    assert [i["id"] for i in items] == [str(newer.id.value), str(older.id.value)]
    assert all(set(i) == SAVED_CV_KEYS for i in items), "no extracted_text in the list"
    own = await http.get("/api/me/base-cvs", headers=target.headers)
    assert items == own.json()["items"], "the same list the owner sees"


async def test_ac3_an_account_with_no_saved_cv_gets_an_empty_list_not_a_404(
    http: AsyncClient, make_account: MakeAccount
) -> None:
    admin, target = await make_account(admin=True), await make_account()

    response = await http.get(f"{ADMIN_USERS}/{target.id}/base-cvs", headers=admin.headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"items": []}
    _no_store(response)


# ----------------------------------------------------------------------------- AC-4 CV read


async def test_ac4_one_cv_has_the_saved_cv_keys_plus_extracted_text_and_no_store(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    cv = extracted_cv(target.owner, _at(clock, 10))
    await _add_cv(world_app, cv, settings, with_file=False)
    assert cv.extracted_text is not None
    stored_text = cv.extracted_text.value  # the value object stores it trimmed

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/base-cvs/{cv.id.value}", headers=admin.headers
    )

    assert response.status_code == 200, response.text
    _no_store(response)
    body = response.json()
    assert set(body) == SAVED_CV_KEYS | {"extracted_text"}
    assert body["extracted_text"] == stored_text


async def test_ac4_a_250000_character_cv_is_returned_whole(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    cv = uploaded_cv(target.owner, _at(clock, 10))
    cv.mark_extracted(ExtractedText("x" * 250_000), _at(clock, 9))
    cv.release_events()
    await _add_cv(world_app, cv, settings, with_file=False)

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/base-cvs/{cv.id.value}", headers=admin.headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["extracted_text"] == "x" * 250_000


async def test_ac4_a_cv_whose_extraction_failed_has_null_text(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    cv = extraction_failed_cv(target.owner, _at(clock, 10))
    await _add_cv(world_app, cv, settings, with_file=False)

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/base-cvs/{cv.id.value}", headers=admin.headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["extracted_text"] is None
    assert response.json()["status"] == "extraction_failed"


# ----------------------------------------------------------------------------- AC-5 postings


async def test_ac5_the_posting_list_is_summaries_newest_first_and_the_owners_own_list(
    http: AsyncClient, make_account: MakeAccount, world_app: FastAPI, clock: FixedClock
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    older = pasted_posting(target.owner, _at(clock, 30), marker="o")
    newer = pasted_posting(target.owner, _at(clock, 10), marker="n")
    await _add_posting(world_app, older)
    await _add_posting(world_app, newer)

    response = await http.get(f"{ADMIN_USERS}/{target.id}/job-postings", headers=admin.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    items = response.json()["items"]
    assert [i["id"] for i in items] == [str(newer.id.value), str(older.id.value)]
    assert all(set(i) == JOB_POSTING_SUMMARY_KEYS and "text" not in i for i in items)
    own = await http.get("/api/me/job-postings", params={"limit": "20"}, headers=target.headers)
    assert items == own.json()["items"]


async def test_ac5_the_posting_limit_defaults_to_20_and_is_bounded_1_to_20(
    http: AsyncClient, make_account: MakeAccount, world_app: FastAPI, clock: FixedClock
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    for minute in range(22):
        await _add_posting(world_app, pasted_posting(target.owner, _at(clock, 60 - minute)))
    url = f"{ADMIN_USERS}/{target.id}/job-postings"

    default = await http.get(url, headers=admin.headers)
    three = await http.get(url, params={"limit": "3"}, headers=admin.headers)
    zero = await http.get(url, params={"limit": "0"}, headers=admin.headers)
    over = await http.get(url, params={"limit": "21"}, headers=admin.headers)

    assert default.status_code == 200, default.text
    assert len(default.json()["items"]) == 20
    assert three.status_code == 200, three.text
    assert len(three.json()["items"]) == 3
    for refused in (zero, over):
        assert refused.status_code == 422, refused.text
        assert error_code(refused) == "validation_error"


async def test_ac5_one_posting_has_its_text_and_no_expiry_and_no_store(
    http: AsyncClient, make_account: MakeAccount, world_app: FastAPI, clock: FixedClock
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    posting = pasted_posting(target.owner, _at(clock, 10), marker="z")
    await _add_posting(world_app, posting)
    stored_text = posting.text.value  # the value object stores it trimmed

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/job-postings/{posting.id.value}", headers=admin.headers
    )

    assert response.status_code == 200, response.text
    _no_store(response)
    body = response.json()
    assert set(body) == JOB_POSTING_RESPONSE_KEYS
    assert body["text"] == stored_text
    assert body["expires_at"] is None


# ----------------------------------------------------------------------------- AC-6 history


async def test_ac6_every_history_page_equals_the_owners_own_page(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    for minutes in (50, 40, 30):
        await _entry(world_app, settings, target.owner, _at(clock, minutes), ready_formats=())
    url = f"{ADMIN_USERS}/{target.id}/tailoring-runs"
    admin_pages: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(3):
        params = {"limit": "2", **({"cursor": cursor} if cursor else {})}
        mine = await http.get(url, params=params, headers=admin.headers)
        own = await http.get("/api/me/tailoring-runs", params=params, headers=target.headers)
        assert mine.status_code == 200, mine.text
        _no_store(mine)
        assert mine.json() == own.json(), "the same HistoryPageResponse for the same data"
        admin_pages.append(mine.json())
        cursor = mine.json()["next_cursor"]
        if cursor is None:
            break

    assert [len(p["items"]) for p in admin_pages] == [2, 1]
    assert all(set(item) == HISTORY_ENTRY_KEYS for p in admin_pages for item in p["items"])
    assert admin_pages[-1]["next_cursor"] is None


async def test_ac6_a_malformed_cursor_is_422_invalid_cursor_and_is_not_echoed(
    http: AsyncClient, make_account: MakeAccount
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    marker = f"CMARKER{uuid4().hex}"

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/tailoring-runs",
        params={"cursor": marker},
        headers=admin.headers,
    )

    assert response.status_code == 422, response.text
    assert error_code(response) == "invalid_cursor"
    assert marker not in response.text
    _no_store(response)


@pytest.mark.parametrize("limit", ["0", "51", "-1", "abc"])
async def test_ac6_a_limit_outside_1_to_50_is_422_validation_error(
    http: AsyncClient, make_account: MakeAccount, limit: str
) -> None:
    admin, target = await make_account(admin=True), await make_account()

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/tailoring-runs", params={"limit": limit}, headers=admin.headers
    )

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"


# ----------------------------------------------------------------------------- AC-7 one run


async def test_ac7_one_run_is_the_owners_own_response_with_both_documents_and_no_store(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10))

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/tailoring-runs/{entry.run_id.value}", headers=admin.headers
    )

    assert response.status_code == 200, response.text
    _no_store(response)
    own = await http.get(f"/api/me/tailoring-runs/{entry.run_id.value}", headers=target.headers)
    assert response.json() == own.json()
    assert response.json()["tailored_cv"]
    assert response.json()["cover_letter"]


@pytest.mark.parametrize("build", [queued_run, running_run, failed_run])
async def test_ac7_a_run_is_readable_in_every_status_queued_included(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    build: Callable[..., Any],
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    run = build(target.owner, _at(clock, 10))
    entry = await _entry(
        world_app, settings, target.owner, _at(clock, 10), run=run, ready_formats=()
    )

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/tailoring-runs/{entry.run_id.value}", headers=admin.headers
    )
    own = await http.get(f"/api/me/tailoring-runs/{entry.run_id.value}", headers=target.headers)

    assert response.status_code == 200, response.text
    assert response.json() == own.json()


# ----------------------------------------------------------------------------- AC-8 inline download


@pytest.mark.parametrize("kind", ["cv", "cover_letter"])
@pytest.mark.parametrize("fmt", ["md", "txt"])
async def test_ac8_the_inline_download_is_the_users_bytes_and_headers(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    kind: str,
    fmt: str,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10), ready_formats=())
    tail = f"{entry.run_id.value}/documents/{kind}/download"

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/tailoring-runs/{tail}",
        params={"format": fmt},
        headers=admin.headers,
    )
    own = await http.get(
        f"/api/me/tailoring-runs/{tail}", params={"format": fmt}, headers=target.headers
    )

    assert response.status_code == 200, response.text
    assert response.content == own.content
    assert response.content
    for header in (
        "cache-control",
        "x-content-type-options",
        "content-disposition",
        "content-type",
    ):
        assert response.headers.get(header) == own.headers.get(header), header
    _no_store(response)


async def test_ac8_a_run_that_has_not_succeeded_is_409_not_exportable_with_its_status(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    run = queued_run(target.owner, _at(clock, 10))
    entry = await _entry(
        world_app, settings, target.owner, _at(clock, 10), run=run, ready_formats=()
    )

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/tailoring-runs/{entry.run_id.value}/documents/cv/download",
        params={"format": "md"},
        headers=admin.headers,
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "tailoring_run_not_exportable"
    assert error_body(response)["status"] == "queued"
    _no_store(response)


async def test_ac8_format_pdf_is_422(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10), ready_formats=())

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/tailoring-runs/{entry.run_id.value}/documents/cv/download",
        params={"format": "pdf"},
        headers=admin.headers,
    )

    assert response.status_code == 422, response.text


# ----------------------------------------------------------------------------- AC-9 exports list


async def test_ac9_the_export_list_points_ready_files_at_the_admin_path_and_otherwise_matches(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(
        world_app,
        settings,
        target.owner,
        _at(clock, 10),
        ready_formats=(ExportFormat.PDF, ExportFormat.DOCX),
    )
    queued = queued_export(target.owner, entry.run, _at(clock, 5), format=ExportFormat.DOCX)
    async with world_app.state.session_factory() as session:
        await SqlAlchemyExportJobRepository(session).add(queued)
        await session.commit()
    run_tail = f"tailoring-runs/{entry.run_id.value}/exports"

    response = await http.get(f"{ADMIN_USERS}/{target.id}/{run_tail}", headers=admin.headers)
    own = await http.get(f"/api/me/{run_tail}", headers=target.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    items = response.json()["items"]
    assert len(items) == 3
    by_id = {i["id"]: i for i in items}
    for job in entry.jobs:
        assert by_id[str(job.id.value)]["file_url"] == (
            f"{ADMIN_USERS}/{target.id}/export-jobs/{job.id.value}/file"
        )
    assert by_id[str(queued.id.value)]["file_url"] is None

    def _without_file_url(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: v for k, v in r.items() if k != "file_url"} for r in rows]

    assert _without_file_url(items) == _without_file_url(own.json()["items"])


# ----------------------------------------------------------------------------- AC-10 export file


async def test_ac10_a_ready_file_is_served_with_the_users_bytes_and_headers(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10))
    job = entry.jobs[0]

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/export-jobs/{job.id.value}/file", headers=admin.headers
    )
    own = await http.get(f"/api/me/export-jobs/{job.id.value}/file", headers=target.headers)

    assert response.status_code == 200, response.text
    assert response.content == own.content == f"%PDF-1.7 export {job.id.value}".encode()
    for header in (
        "cache-control",
        "x-content-type-options",
        "content-disposition",
        "content-type",
    ):
        assert response.headers.get(header) == own.headers.get(header), header
    _no_store(response)


async def test_ac10_an_older_export_is_still_served(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    engine: AsyncEngine,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10))
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE tailoring_run SET version = version + 1 WHERE id = :i"),
            {"i": entry.run_id.value},
        )
    listing = await http.get(
        f"{ADMIN_USERS}/{target.id}/tailoring-runs/{entry.run_id.value}/exports",
        headers=admin.headers,
    )
    assert listing.status_code == 200, listing.text
    assert listing.json()["items"][0]["current"] is False, "premise: the export is older"

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/export-jobs/{entry.jobs[0].id.value}/file",
        headers=admin.headers,
    )

    assert response.status_code == 200, response.text
    assert response.content == f"%PDF-1.7 export {entry.jobs[0].id.value}".encode()


@pytest.mark.parametrize("state", ["queued", "failed"])
async def test_ac10_a_job_that_is_not_ready_is_409_export_not_ready(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    state: str,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10), ready_formats=())
    job = queued_export(target.owner, entry.run, _at(clock, 5), format=ExportFormat.DOCX)
    if state == "failed":
        job.mark_started(_at(clock, 4))
        job.mark_failed(ExportFailureReason.RENDER_FAILED, _at(clock, 3))
        job.release_events()
    async with world_app.state.session_factory() as session:
        await SqlAlchemyExportJobRepository(session).add(job)
        await session.commit()

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/export-jobs/{job.id.value}/file", headers=admin.headers
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "export_not_ready"
    assert error_body(response)["status"] == state
    _no_store(response)


async def test_ac10_a_ready_row_whose_file_is_gone_is_410_export_file_gone(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10))
    (settings.upload_dir / entry.jobs[0].storage_ref.key).unlink()

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/export-jobs/{entry.jobs[0].id.value}/file",
        headers=admin.headers,
    )

    assert response.status_code == 410, response.text
    assert error_code(response) == "export_file_gone"
    _no_store(response)


async def test_ac10_a_failing_file_store_is_503_storage_unavailable(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected below `LocalFileStore.get`'s own `except OSError` floor, at its synchronous read."""
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10))

    def _eio(self: LocalFileStore, ref: FileRef) -> bytes:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(LocalFileStore, "_get_sync", _eio)

    response = await http.get(
        f"{ADMIN_USERS}/{target.id}/export-jobs/{entry.jobs[0].id.value}/file",
        headers=admin.headers,
    )

    assert response.status_code == 503, response.text
    assert error_code(response) == "storage_unavailable"
    _no_store(response)


# ----------------------------------------------------------------------------- AC-11 delete a CV


def _cv_path(settings: Settings, cv_id: BaseCvId) -> Path:
    return settings.upload_dir / FileRef.for_base_cv(cv_id, CvContentType.PDF).key


async def test_ac11_deleting_a_cv_commits_the_row_unlinks_the_file_and_history_says_cv_deleted(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    engine: AsyncEngine,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10), ready_formats=())
    await LocalFileStore(settings.upload_dir).put(
        FileRef.for_base_cv(entry.cv_id, CvContentType.PDF), b"original upload bytes"
    )
    path = _cv_path(settings, entry.cv_id)
    assert path.exists(), "premise: the file is on disk"

    response = await http.delete(
        f"{ADMIN_USERS}/{target.id}/base-cvs/{entry.cv_id.value}", headers=admin.headers
    )

    assert response.status_code == 200, response.text
    _no_store(response)
    assert response.json() == {"file_unlinked": True}
    assert await _rows(engine, "intake_base_cv", entry.cv_id.value) == 0
    assert not path.exists()
    history = await http.get(f"{ADMIN_USERS}/{target.id}/tailoring-runs", headers=admin.headers)
    assert history.json()["items"][0]["base_cv"] is None, "history reads CV deleted"


async def test_ac11_a_failing_unlink_is_200_file_unlinked_false_with_the_row_gone(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10), ready_formats=())
    await LocalFileStore(settings.upload_dir).put(
        FileRef.for_base_cv(entry.cv_id, CvContentType.PDF), b"original upload bytes"
    )
    failing = str(_cv_path(settings, entry.cv_id))
    original = os.unlink

    def _selective(path: Any, *args: Any, **kwargs: Any) -> None:
        if os.fspath(path) == failing:
            raise OSError(5, "Input/output error")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", _selective)

    response = await http.delete(
        f"{ADMIN_USERS}/{target.id}/base-cvs/{entry.cv_id.value}", headers=admin.headers
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"file_unlinked": False}
    assert await _rows(engine, "intake_base_cv", entry.cv_id.value) == 0
    assert os.path.exists(failing)  # noqa: ASYNC240 -- one stat, test-only, "the failing unlink left its bytes for the orphan sweep"


async def test_ac11_a_second_cv_delete_is_404_base_cv_not_found(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    cv = extracted_cv(target.owner, _at(clock, 10))
    await _add_cv(world_app, cv, settings, with_file=True)
    url = f"{ADMIN_USERS}/{target.id}/base-cvs/{cv.id.value}"
    first = await http.delete(url, headers=admin.headers)
    assert first.status_code == 200, first.text

    second = await http.delete(url, headers=admin.headers)

    assert second.status_code == 404, second.text
    assert error_code(second) == "base_cv_not_found"
    _no_store(second)


# ----------------------------------------------------------------------------- AC-12 delete an entry


async def test_ac12_deleting_an_entry_takes_the_run_exports_card_and_posting_and_reports_counts(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    engine: AsyncEngine,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(
        world_app,
        settings,
        target.owner,
        _at(clock, 10),
        ready_formats=(ExportFormat.PDF, ExportFormat.DOCX),
    )
    card = await http.post(
        "/api/me/tracked-applications",
        json={"tailoring_run_id": str(entry.run_id.value)},
        headers=target.headers,
    )
    assert card.status_code == 201, card.text
    paths = [settings.upload_dir / job.storage_ref.key for job in entry.jobs]
    assert all(p.exists() for p in paths), "premise: the export files exist"

    response = await http.delete(
        f"{ADMIN_USERS}/{target.id}/tailoring-runs/{entry.run_id.value}", headers=admin.headers
    )

    assert response.status_code == 200, response.text
    _no_store(response)
    body = response.json()
    assert set(body) == ERASURE_KEYS
    assert all(type(v) is int for v in body.values()), body
    assert body == {
        "tailoring_runs": 1,
        "job_postings": 1,
        "export_jobs": 2,
        "tracked_applications": 1,
        "files_unlinked": 2,
        "unlink_failures": 0,
    }
    assert await _rows(engine, "tailoring_run", entry.run_id.value) == 0
    assert await _rows(engine, "posting_job_posting", entry.posting_id.value) == 0
    assert (
        await _count(
            engine,
            "SELECT count(*) FROM export_job WHERE tailoring_run_id = :r",
            r=entry.run_id.value,
        )
        == 0
    )
    assert not any(p.exists() for p in paths)
    assert await _rows(engine, "intake_base_cv", entry.cv_id.value) == 1, "the saved CV stays"


@pytest.mark.parametrize(("build", "state"), [(queued_run, "queued"), (running_run, "running")])
async def test_ac12_an_entry_in_progress_is_409_with_its_status_and_nothing_deleted(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    engine: AsyncEngine,
    build: Callable[..., Any],
    state: str,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    run = build(target.owner, _at(clock, 10))
    entry = await _entry(
        world_app, settings, target.owner, _at(clock, 10), run=run, ready_formats=()
    )

    response = await http.delete(
        f"{ADMIN_USERS}/{target.id}/tailoring-runs/{entry.run_id.value}", headers=admin.headers
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "tailoring_run_in_progress"
    assert error_body(response)["status"] == state
    _no_store(response)
    assert await _rows(engine, "tailoring_run", entry.run_id.value) == 1
    assert await _rows(engine, "posting_job_posting", entry.posting_id.value) == 1


async def test_ac12_a_failing_unlink_is_200_counted_in_unlink_failures_with_the_rows_gone(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(
        world_app,
        settings,
        target.owner,
        _at(clock, 10),
        ready_formats=(ExportFormat.PDF, ExportFormat.DOCX),
    )
    failing = str(settings.upload_dir / entry.jobs[0].storage_ref.key)
    original = os.unlink

    def _selective(path: Any, *args: Any, **kwargs: Any) -> None:
        if os.fspath(path) == failing:
            raise OSError(5, "Input/output error")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", _selective)

    response = await http.delete(
        f"{ADMIN_USERS}/{target.id}/tailoring-runs/{entry.run_id.value}", headers=admin.headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert type(body["unlink_failures"]) is int
    assert body["unlink_failures"] > 0
    assert await _rows(engine, "tailoring_run", entry.run_id.value) == 0
    assert os.path.exists(failing)  # noqa: ASYNC240 -- one stat, test-only


async def test_ac12_a_second_entry_delete_is_404_tailoring_run_not_found(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10), ready_formats=())
    url = f"{ADMIN_USERS}/{target.id}/tailoring-runs/{entry.run_id.value}"
    first = await http.delete(url, headers=admin.headers)
    assert first.status_code == 200, first.text

    second = await http.delete(url, headers=admin.headers)

    assert second.status_code == 404, second.text
    assert error_code(second) == "tailoring_run_not_found"
    _no_store(second)


# ----------------------------------------------------------------------------- AC-13 providers


def test_ac13_the_two_deletes_use_the_same_providers_as_the_users_own_routes(app: FastAPI) -> None:
    by_key = {
        (method, route.path): route
        for route in _iter_api_routes(app.routes)
        for method in route.methods or ()
    }
    pairs = [
        (
            ("DELETE", "/api/admin/users/{user_id}/base-cvs/{base_cv_id}"),
            ("DELETE", "/api/me/base-cvs/{base_cv_id}"),
            get_delete_saved_base_cv,
        ),
        (
            ("DELETE", "/api/admin/users/{user_id}/tailoring-runs/{tailoring_run_id}"),
            ("DELETE", "/api/me/tailoring-runs/{tailoring_run_id}"),
            get_erase_history_entry,
        ),
    ]
    for admin_key, user_key, provider in pairs:
        assert provider in _all_dependency_calls(by_key[admin_key].dependant), admin_key
        assert provider in _all_dependency_calls(by_key[user_key].dependant), user_key


# ----------------------------------------------------------------------------- AC-14 owner equality


@pytest_asyncio.fixture
async def foreign(
    world_app: FastAPI,
    settings: Settings,
    make_account: MakeAccount,
    clock: FixedClock,
    seeded_ids: list[UUID],
) -> dict[str, Entry]:
    """A full entry owned by another user, and one owned by a guest session."""
    other = await make_account()
    other_entry = await _entry(world_app, settings, other.owner, _at(clock, 20))
    async with new_client(world_app) as guest_client, world_app.state.session_factory() as session:
        guest = await mint_guest(guest_client, session)
    _GUESTS.append(guest.guest_session_id.value)
    guest_entry = await _entry(world_app, settings, guest, _at(clock, 20))
    return {"other-user": other_entry, "guest": guest_entry}


@pytest.mark.parametrize("kind", ["other-user", "guest"])
@pytest.mark.parametrize("route", ITEM_ROUTES)
async def test_ac14_a_row_the_path_user_does_not_own_is_the_kinds_404_and_nothing_is_deleted(
    http: AsyncClient,
    make_account: MakeAccount,
    foreign: dict[str, Entry],
    engine: AsyncEngine,
    kind: str,
    route: str,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = foreign[kind]
    method, url = _entry_routes(target.id, entry)[route]

    response = await http.request(method, url, headers=admin.headers)

    assert response.status_code == 404, response.text
    assert error_code(response) == ITEM_404[route]
    _no_store(response)
    assert await _rows(engine, "intake_base_cv", entry.cv_id.value) == 1
    assert await _rows(engine, "tailoring_run", entry.run_id.value) == 1
    assert await _rows(engine, "posting_job_posting", entry.posting_id.value) == 1


@pytest.mark.parametrize("route", ITEM_ROUTES)
async def test_ac14_an_id_that_does_not_exist_is_the_same_404_as_a_foreign_one(
    http: AsyncClient, make_account: MakeAccount, foreign: dict[str, Entry], route: str
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    method, missing_url = _random_routes(target.id)[route]
    _, foreign_url = _entry_routes(target.id, foreign["other-user"])[route]

    missing = await http.request(method, missing_url, headers=admin.headers)
    refused = await http.request(method, foreign_url, headers=admin.headers)

    assert missing.status_code == refused.status_code == 404
    assert error_code(missing) == error_code(refused) == ITEM_404[route]
    assert missing.json() == refused.json(), "neither existence nor owner is revealed"


# ----------------------------------------------------------------------------- AC-15 missing target


@pytest.mark.parametrize("route", ROUTE_NAMES)
async def test_ac15_an_unknown_target_is_404_user_not_found_never_401(
    http: AsyncClient, make_account: MakeAccount, route: str
) -> None:
    admin = await make_account(admin=True)
    method, url = _random_routes(uuid4())[route]

    response = await http.request(method, url, headers=admin.headers)

    assert response.status_code == 404, response.text
    assert error_code(response) == "user_not_found"
    _no_store(response)


async def test_ac15_a_target_erased_mid_session_is_404_user_not_found_on_all_eleven_and_the_admin_stays_signed_in(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10))
    erased = await http.delete(f"{ADMIN_USERS}/{target.id}", headers=admin.headers)
    assert erased.status_code == 200, erased.text

    for name, (method, url) in _entry_routes(target.id, entry).items():
        response = await http.request(method, url, headers=admin.headers)
        assert response.status_code == 404, f"{name}: {response.text}"
        assert error_code(response) == "user_not_found", name

    still_admin = await http.get(ADMIN_USERS, headers=admin.headers)
    assert still_admin.status_code == 200, "the admin was not signed out"


# ----------------------------------------------------------------------------- AC-16 firewall


def _firewalled_urls(uid: object) -> list[tuple[str, str]]:
    """Every route, valid-shaped and malformed in every parameter."""
    cv, post, run, job = uuid4(), uuid4(), uuid4(), uuid4()
    urls = list(_routes(uid, cv, post, run, job).values())
    urls += list(_routes("not-a-uuid", cv, post, run, job).values())
    urls += list(_routes(uid, "not-a-uuid", "not-a-uuid", "not-a-uuid", "not-a-uuid").values())
    p = f"{ADMIN_USERS}/{uid}"
    urls += [
        ("GET", f"{p}/tailoring-runs?cursor=!!&limit=999"),
        ("GET", f"{p}/tailoring-runs?cursor={'a' * 300}"),
        ("GET", f"{p}/job-postings?limit=0"),
        ("GET", f"{p}/job-postings?limit=21"),
        ("GET", f"{p}/tailoring-runs/{run}/documents/cv/download?format=pdf"),
        ("GET", f"{p}/tailoring-runs/{run}/documents/cv/download"),
        ("GET", f"{p}/tailoring-runs/{run}/documents/resume/download?format=md"),
    ]
    return urls


async def test_ac16_a_plain_user_gets_the_404_of_an_unmatched_path_on_every_route_and_variant(
    http: AsyncClient, make_account: MakeAccount
) -> None:
    user = await make_account()
    unmatched = await http.get(UNMATCHED, headers=user.headers)
    assert unmatched.status_code == 404, unmatched.text

    for method, url in _firewalled_urls(uuid4()):
        refused = await http.request(method, url, headers=user.headers)
        assert refused.status_code == 404, f"{method} {url}: {refused.text}"
        assert refused.content == unmatched.content, f"{method} {url}"
        assert refused.json() == {"detail": "Not Found"}
        assert _comparable(refused) == _comparable(unmatched), f"{method} {url}"


async def test_ac16_anonymous_is_401_invalid_access_token_on_all_eleven(http: AsyncClient) -> None:
    for name, (method, url) in _random_routes(uuid4()).items():
        response = await http.request(method, url)
        assert response.status_code == 401, f"{name}: {response.text}"
        assert error_code(response) == "invalid_access_token", name


async def test_ac16_a_disabled_admin_is_403_account_disabled_on_all_eleven(
    http: AsyncClient, make_account: MakeAccount
) -> None:
    disabled, other_admin = await make_account(admin=True), await make_account(admin=True)
    done = await http.post(f"{ADMIN_USERS}/{disabled.id}/disable", headers=other_admin.headers)
    assert done.status_code == 200, done.text

    for name, (method, url) in _random_routes(uuid4()).items():
        response = await http.request(method, url, headers=disabled.headers)
        assert response.status_code == 403, f"{name}: {response.text}"
        assert error_code(response) == "account_disabled", name


# ----------------------------------------------------------------------------- AC-17 acting as self


async def test_ac17_a_plain_user_calling_their_own_id_under_admin_gets_the_firewalls_404(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    user = await make_account()
    entry = await _entry(world_app, settings, user.owner, _at(clock, 10))
    unmatched = await http.get(UNMATCHED, headers=user.headers)

    for name, (method, url) in _entry_routes(user.id, entry).items():
        response = await http.request(method, url, headers=user.headers)
        assert response.status_code == 404, f"{name}: {response.text}"
        assert response.content == unmatched.content, name
    own = await http.get(f"/api/me/tailoring-runs/{entry.run_id.value}", headers=user.headers)
    assert own.status_code == 200, "the owner's own surface is untouched"


# ----------------------------------------------------------------------------- AC-18 disabled target


async def test_ac18_a_disabled_targets_content_is_readable_and_deletable(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    engine: AsyncEngine,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10))
    disabled = await http.post(f"{ADMIN_USERS}/{target.id}/disable", headers=admin.headers)
    assert disabled.status_code == 200, disabled.text
    routes = _entry_routes(target.id, entry)

    for name in ROUTE_NAMES:
        method, url = routes[name]
        if method == "DELETE":
            continue
        response = await http.request(method, url, headers=admin.headers)
        assert response.status_code == 200, f"{name}: {response.text}"
    cv = await http.delete(routes["delete_cv"][1], headers=admin.headers)
    run = await http.delete(routes["delete_run"][1], headers=admin.headers)

    assert cv.status_code == 200, cv.text
    assert run.status_code == 200, run.text
    assert await _rows(engine, "intake_base_cv", entry.cv_id.value) == 0
    assert await _rows(engine, "tailoring_run", entry.run_id.value) == 0


# ----------------------------------------------------------------------------- AC-19 infrastructure

_CONTENT_TABLES = ("intake_base_cv", "posting_job_posting", "tailoring_run", "export_job")


@pytest.mark.parametrize("route", ROUTE_NAMES)
async def test_ac19_postgres_failing_below_the_repository_is_503_never_404_or_200(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    """Fails any statement that touches a content table, so the handler's own read is what breaks.

    The target is a real account with content: for an unknown one the correct answer is 404
    `user_not_found`, decided before any content table is read."""
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10))
    method, url = _entry_routes(target.id, entry)[route]
    # The sync `Session.execute` is what `AsyncSession.execute` *and* `AsyncSession.get` reach.
    original = Session.execute

    def _down(self: Session, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if any(table in str(statement) for table in _CONTENT_TABLES):
            raise SQLAlchemyError("simulated: postgres unavailable (AC-19)")
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", _down)

    response = await http.request(method, url, headers=admin.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"


async def test_ac19_a_failing_database_during_the_bearer_check_is_503_on_all_eleven(
    http: AsyncClient, make_account: MakeAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await make_account(admin=True)

    async def _down(self: AsyncSession, *args: object, **kwargs: object) -> None:
        raise SQLAlchemyError("simulated: postgres unavailable (AC-19)")

    monkeypatch.setattr(AsyncSession, "execute", _down)

    for name, (method, url) in _random_routes(uuid4()).items():
        response = await http.request(method, url, headers=admin.headers)
        assert response.status_code == 503, f"{name}: {response.text}"
        assert error_code(response) == "service_unavailable", name


@pytest.mark.parametrize("route", ["delete_cv", "delete_run"])
async def test_ac19_a_delete_whose_commit_fails_is_503_with_nothing_deleted_and_no_file_touched(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10))
    await LocalFileStore(settings.upload_dir).put(
        FileRef.for_base_cv(entry.cv_id, CvContentType.PDF), b"original upload bytes"
    )
    method, url = _entry_routes(target.id, entry)[route]

    async def _commit_fails(self: AsyncSession) -> None:
        raise SQLAlchemyError("simulated commit failure (AC-19)")

    monkeypatch.setattr(AsyncSession, "commit", _commit_fails)

    response = await http.request(method, url, headers=admin.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"
    assert await _rows(engine, "intake_base_cv", entry.cv_id.value) == 1
    assert await _rows(engine, "tailoring_run", entry.run_id.value) == 1
    assert _cv_path(settings, entry.cv_id).exists()
    assert all((settings.upload_dir / j.storage_ref.key).exists() for j in entry.jobs)


async def test_ac19_with_redis_unreachable_every_content_route_answers_as_normal(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10))
    override_settings(world_app, settings, redis_url="redis://127.0.0.1:1/0")

    routes = _entry_routes(target.id, entry)
    statuses = {}
    for name in sorted(ROUTE_NAMES, key=lambda n: routes[n][0] == "DELETE"):  # deletes last
        method, url = routes[name]
        statuses[name] = (await http.request(method, url, headers=admin.headers)).status_code

    assert statuses == dict.fromkeys(ROUTE_NAMES, 200), statuses


# ============================================================================= T7 (test-after)
# AC-20 ... AC-24: the access log, privacy, the guest cookie, no-store everywhere, the schemas.

ACCESS_EVENT = "admin.content_access"
EXPECTED_ACCESS = {
    "list_cvs": ("base_cv", "list", None),
    "get_cv": ("base_cv", "read", "cv"),
    "delete_cv": ("base_cv", "delete", "cv"),
    "list_postings": ("job_posting", "list", None),
    "get_posting": ("job_posting", "read", "posting"),
    "list_history": ("history", "list", None),
    "get_run": ("tailoring_run", "read", "run"),
    "download": ("tailoring_run", "download", "run"),
    "list_exports": ("export_job", "list", None),
    "delete_run": ("history", "delete", "run"),
    "export_file": ("export_job", "download", "job"),
}


def _lines(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if event in r.getMessage()]


def _ordered(routes: dict[str, tuple[str, str]]) -> list[str]:
    return sorted(ROUTE_NAMES, key=lambda n: routes[n][0] == "DELETE")  # deletes last


async def test_ac20_each_route_writes_exactly_one_access_line_with_its_fields(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10))
    ids = {
        "cv": str(entry.cv_id.value),
        "posting": str(entry.posting_id.value),
        "run": str(entry.run_id.value),
        "job": str(entry.jobs[0].id.value),
    }
    routes = _entry_routes(target.id, entry)

    for name in _ordered(routes):
        method, url = routes[name]
        caplog.clear()
        with caplog.at_level(logging.INFO):
            response = await http.request(method, url, headers=admin.headers)
        assert response.status_code == 200, f"{name}: {response.text}"
        lines = _lines(caplog, ACCESS_EVENT)
        assert len(lines) == 1, f"{name}: {lines}"
        kind, action, item = EXPECTED_ACCESS[name]
        line = lines[0]
        assert line["actor_id"] == str(admin.id), name
        assert line["target_user_id"] == str(target.id), name
        assert (line["kind"], line["action"], line["outcome"]) == (kind, action, "ok"), name
        assert line.get("item_id") == (ids[item] if item else None), name
        assert "item_id" not in line or item is not None, f"{name}: lists carry no item_id"
        assert str(admin.id) in caplog.text
        reads_only = [r for r in caplog.records if str(target.id) in r.getMessage()]
        if method == "GET":
            assert len(reads_only) == 1, f"{name}: nothing else mentions the target"


async def test_ac20_refusals_log_not_found_or_refused_with_the_item_id(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    missing_cv = uuid4()
    run = queued_run(target.owner, _at(clock, 10))
    entry = await _entry(
        world_app, settings, target.owner, _at(clock, 10), run=run, ready_formats=()
    )

    with caplog.at_level(logging.INFO):
        gone = await http.get(
            f"{ADMIN_USERS}/{target.id}/base-cvs/{missing_cv}", headers=admin.headers
        )
        busy = await http.delete(
            f"{ADMIN_USERS}/{target.id}/tailoring-runs/{entry.run_id.value}", headers=admin.headers
        )
        nobody = await http.get(f"{ADMIN_USERS}/{uuid4()}/base-cvs", headers=admin.headers)

    assert (gone.status_code, busy.status_code, nobody.status_code) == (404, 409, 404)
    outcomes = [
        (line["kind"], line["action"], line["outcome"], line.get("item_id"))
        for line in _lines(caplog, ACCESS_EVENT)
    ]
    assert outcomes == [
        ("base_cv", "read", "not_found", str(missing_cv)),
        ("history", "delete", "refused", str(entry.run_id.value)),
        ("base_cv", "list", "not_found", None),
    ]


async def _entry_with_a_blocked_export(
    world_app: FastAPI, settings: Settings, owner: Owner, at: datetime
) -> Entry:
    entry = await _entry(
        world_app, settings, owner, at, ready_formats=(ExportFormat.PDF, ExportFormat.DOCX)
    )
    blocked = settings.upload_dir / entry.jobs[0].storage_ref.key
    blocked.unlink()
    blocked.mkdir()
    (blocked / "keep").write_bytes(b"x")
    return entry


def _without_ids(line: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in line.items() if k not in {"tailoring_run_id", "timestamp"}}


async def test_ac20_the_deletes_write_the_users_erasure_and_unlink_lines_unchanged(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same entry shape deleted from both seats writes the same lines (ids aside)."""
    admin, target = await make_account(admin=True), await make_account()
    by_admin = await _entry_with_a_blocked_export(world_app, settings, target.owner, _at(clock, 20))
    by_user = await _entry_with_a_blocked_export(world_app, settings, target.owner, _at(clock, 10))

    with caplog.at_level(logging.INFO):
        mine = await http.delete(
            f"{ADMIN_USERS}/{target.id}/tailoring-runs/{by_admin.run_id.value}",
            headers=admin.headers,
        )
    admin_erased = _lines(caplog, "retention.history_entry_erased")
    admin_failed = _lines(caplog, "retention.history_entry_file_unlink_failed")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        theirs = await http.delete(
            f"/api/me/tailoring-runs/{by_user.run_id.value}", headers=target.headers
        )
    user_erased = _lines(caplog, "retention.history_entry_erased")
    user_failed = _lines(caplog, "retention.history_entry_file_unlink_failed")

    assert mine.status_code == 200, mine.text
    assert theirs.status_code == 204, theirs.text
    assert len(admin_erased) == len(user_erased) == 1
    assert _without_ids(admin_erased[0]) == _without_ids(user_erased[0])
    assert admin_erased[0]["tailoring_run_id"] == str(by_admin.run_id.value)
    assert len(admin_failed) == len(user_failed) == 1
    assert _without_ids(admin_failed[0]) == _without_ids(user_failed[0])


# --- AC-21: planted markers --------------------------------------------------------------------


async def _marked_entry(
    world_app: FastAPI, settings: Settings, owner: UserOwner, at: datetime, marks: dict[str, str]
) -> tuple[Entry, ExportJobId]:
    cv_id = BaseCvId(uuid4())
    cv = BaseCv.upload(
        id=cv_id,
        owner=owner,
        original_filename=OriginalFilename(f"{marks['filename']}.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=1024,
        file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
        uploaded_at=at,
    )
    cv.mark_extracted(ExtractedText(f"{marks['cv_text']} " * 30), at)
    cv.rename(BaseCvLabel(marks["label"]), at)
    cv.release_events()
    posting = JobPosting.from_fetched_url(
        id=JobPostingId(uuid4()),
        owner=owner,
        url=SourceUrl(f"https://example.com/{marks['url']}"),
        fetched=FetchedPosting(
            text=JobPostingText(f"{marks['posting_text']} " * 30),
            title=PostingTitle(marks["title"]),
        ),
        created_at=at,
    )
    posting.release_events()
    run = running_run(owner, at, base_cv_id=cv.id, job_posting_id=posting.id)
    run.mark_succeeded(
        TailoredDocuments(
            cv=TailoredCv(f"{marks['draft']} " + "c" * 500), cover_letter=CoverLetter("l" * 300)
        ),
        a_draft().metrics,
        at,
    )
    run.release_events()
    run.revise_cv(
        TailoredCv(f"{marks['revision']} " + "r" * 500), expected_version=run.version, at=at
    )
    run.release_events()
    job = ready_export(owner, run, at)
    async with world_app.state.session_factory() as session:
        await SqlAlchemyBaseCvRepository(session).add(cv)
        await SqlAlchemyJobPostingRepository(session).add(posting)
        await SqlAlchemyTailoringRunRepository(session).add(run)
        await SqlAlchemyExportJobRepository(session).add(job)
        await session.commit()
    files = LocalFileStore(settings.upload_dir)
    await files.put(cv.file, b"original upload bytes")
    await files.put(job.storage_ref, export_bytes(job.id))
    entry = Entry(
        cv_id=cv.id,
        posting_id=posting.id,
        posting_preview="",
        run_id=run.id,
        run_version=run.version,
        jobs=[],
        run=run,
    )
    return entry, job.id


async def test_ac21_no_planted_marker_reaches_a_log_record_over_a_full_run_but_ids_do(
    http: AsyncClient,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    engine: AsyncEngine,
    seeded_ids: list[UUID],
    make_account: MakeAccount,
    caplog: pytest.LogCaptureFixture,
) -> None:
    admin = await make_account(admin=True)
    tag = uuid4().hex[:12]
    marks = {
        k: f"MK{k.upper().replace('_', '')}{tag}"
        for k in (
            "filename",
            "cv_text",
            "label",
            "posting_text",
            "title",
            "url",
            "draft",
            "revision",
        )
    }
    email = f"mkemail{tag}@example.com"
    client = new_client(world_app)
    try:
        _token, target_id = await seed_user_and_sign_in(client, settings, email=email)
        seeded_ids.append(target_id)
        owner = UserOwner(UserId(target_id))
        entry, job_id = await _marked_entry(world_app, settings, owner, _at(clock, 10), marks)
        p = f"{ADMIN_USERS}/{target_id}"
        run_p = f"{p}/tailoring-runs/{entry.run_id.value}"
        steps = [
            ("GET", f"{p}/base-cvs"),
            ("GET", f"{p}/base-cvs/{entry.cv_id.value}"),
            ("GET", f"{p}/job-postings"),
            ("GET", f"{p}/job-postings/{entry.posting_id.value}"),
            ("GET", f"{p}/tailoring-runs"),
            ("GET", run_p),
            ("GET", f"{run_p}/documents/cv/download?format=md"),
            ("GET", f"{run_p}/documents/cv/download?format=txt"),
            ("GET", f"{run_p}/exports"),
            ("GET", f"{p}/export-jobs/{job_id.value}/file"),
            ("GET", p),
            ("DELETE", f"{p}/base-cvs/{entry.cv_id.value}"),
            ("DELETE", run_p),
        ]
        bodies: list[str] = []
        with caplog.at_level(logging.DEBUG):
            for method, url in steps:
                response = await http.request(method, url, headers=admin.headers)
                assert response.status_code == 200, f"{method} {url}: {response.text}"
                bodies.append(response.text)
    finally:
        await client.aclose()

    log_text = caplog.text
    for name, marker in [*marks.items(), ("email", email)]:
        assert marker not in log_text, f"{name} reached a log record"
    served = "\n".join(bodies)
    for name in ("filename", "cv_text", "label", "posting_text", "title", "url", "revision"):
        assert marks[name] in served, f"{name} should be in a response body (the reads were real)"
    assert email in served
    assert marks["draft"] not in served, "the model's draft is never served, only the revision"
    for identifier in (
        str(admin.id),
        str(target_id),
        str(entry.cv_id.value),
        str(entry.run_id.value),
    ):
        assert identifier in log_text, "ids are the positive control"


# --- AC-22: the guest cookie is never read -----------------------------------------------------


async def test_ac22_every_content_route_answers_identically_with_and_without_a_guest_cookie(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10))
    async with new_client(world_app) as guest_client, world_app.state.session_factory() as session:
        guest = await mint_guest(guest_client, session)
        guest_cookie = guest_client.cookies.get("__Host-tc_guest")
    _GUESTS.append(guest.guest_session_id.value)
    assert guest_cookie, "premise: a real guest cookie"
    routes = _entry_routes(target.id, entry)

    for name in _ordered(routes):
        method, url = routes[name]
        if method == "DELETE":
            continue
        plain = await http.request(method, url, headers=admin.headers)
        with_cookie = await http.request(
            method, url, headers={**admin.headers, "Cookie": f"__Host-tc_guest={guest_cookie}"}
        )
        assert plain.status_code == with_cookie.status_code == 200, name
        assert plain.content == with_cookie.content, name
        assert "set-cookie" not in with_cookie.headers, f"{name}: nothing is minted or cleared"


# --- AC-23: no-store everywhere ----------------------------------------------------------------


async def test_ac23_every_response_a_handler_builds_carries_no_store_success_or_refusal(
    http: AsyncClient,
    make_account: MakeAccount,
    world_app: FastAPI,
    settings: Settings,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, target = await make_account(admin=True), await make_account()
    entry = await _entry(world_app, settings, target.owner, _at(clock, 10))
    busy = await _entry(
        world_app,
        settings,
        target.owner,
        _at(clock, 20),
        run=queued_run(target.owner, _at(clock, 20)),
        ready_formats=(),
    )
    queued_job = queued_export(target.owner, busy.run, _at(clock, 5), format=ExportFormat.DOCX)
    async with world_app.state.session_factory() as session:
        await SqlAlchemyExportJobRepository(session).add(queued_job)
        await session.commit()
    gone = await _entry(world_app, settings, target.owner, _at(clock, 30))
    (settings.upload_dir / gone.jobs[0].storage_ref.key).unlink()
    p = f"{ADMIN_USERS}/{target.id}"
    requests: list[tuple[str, str, str]] = []
    # refusals: unknown target on all eleven, unknown item on the eight item routes
    for name, (method, url) in _random_routes(uuid4()).items():
        requests.append((f"unknown-target {name}", method, url))
    for name in ITEM_ROUTES:
        method, url = _random_routes(target.id)[name]
        requests.append((f"unknown-item {name}", method, url))
    requests += [
        (
            "not-exportable",
            "GET",
            f"{p}/tailoring-runs/{busy.run_id.value}/documents/cv/download?format=md",
        ),
        ("export-not-ready", "GET", f"{p}/export-jobs/{queued_job.id.value}/file"),
        ("file-gone", "GET", f"{p}/export-jobs/{gone.jobs[0].id.value}/file"),
        ("in-progress", "DELETE", f"{p}/tailoring-runs/{busy.run_id.value}"),
    ]

    def _eio(self: LocalFileStore, ref: FileRef) -> bytes:
        raise OSError(5, "Input/output error")

    with monkeypatch.context() as patched:
        patched.setattr(LocalFileStore, "_get_sync", _eio)
        unavailable = await http.get(
            f"{p}/export-jobs/{entry.jobs[0].id.value}/file", headers=admin.headers
        )
        assert unavailable.status_code == 503, unavailable.text
        _no_store(unavailable)

    # successes on every route, JSON and binary (deletes last)
    ok = _entry_routes(target.id, entry)
    requests += [(f"ok {n}", ok[n][0], ok[n][1]) for n in _ordered(ok)]

    for label, method, url in requests:
        response = await http.request(method, url, headers=admin.headers)
        assert response.status_code != 500, f"{label}: {response.text}"
        _no_store(response)


# --- AC-24: the schemas ------------------------------------------------------------------------


def test_ac24_the_user_schemas_are_unchanged_and_the_admin_one_is_the_saved_one_plus_text() -> None:
    from tailorcraft.infrastructure.api.schemas.admin import AdminSavedBaseCvResponse
    from tailorcraft.infrastructure.api.schemas.intake import (
        BaseCvResponse,
        SavedBaseCvResponse,
    )

    assert set(SavedBaseCvResponse.model_fields) == SAVED_CV_KEYS
    assert set(BaseCvResponse.model_fields) == {
        "character_count",
        "content_type",
        "expires_at",
        "failure_message",
        "failure_reason",
        "id",
        "origin",
        "original_filename",
        "size_bytes",
        "status",
        "uploaded_at",
    }
    assert set(AdminSavedBaseCvResponse.model_fields) == SAVED_CV_KEYS | {"extracted_text"}
    doc = AdminSavedBaseCvResponse.__doc__ or ""
    assert "ADR-0034" in doc
    assert "SavedBaseCvResponse" in doc
    assert "no text" in doc, "names the contradiction with the parent's 'no text and no bytes'"
