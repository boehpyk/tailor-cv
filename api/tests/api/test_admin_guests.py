"""Slice 4.4, T14 RED: the admin guest-sessions router's HTTP contract (AC-9 ... AC-14, AC-23).

Written from the spec (`docs/specs/admin-guests-and-storage/feature-spec.md`, technical plan §0.2,
§4) against the T13 skeleton, whose nine handlers raise `NotImplementedError`. `new_client` uses
`raise_app_exceptions=False`, so the skeleton's failure is a real 500 and a red reads
`assert 500 == 200`.

AC-11 and AC-13 are compared with the guest's **own** routes on one seed: a guest client carrying
the session's real `__Host-tc_guest` — the strongest proof that the admin route is the same use case
from another seat. The administrator's own browser cookie (a different session's) is never the
subject: AC-11 sends one and still reads the path's session.

Regression guards (green against the skeleton, by construction, because the router-level
`require_admin` runs before any handler): the firewall rows of AC-23 and FastAPI's own 422s on a
malformed id. They pin existing behaviour and are listed in the commit body.

Sessions are written through the real repository, rows through `seed_entry`, both **committing**; the
volume is a per-test directory. Cross-request facts are read on a fresh `engine.connect()`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.domain.export.value_objects import ExportFailureReason, ExportFormat
from tailorcraft.domain.intake.value_objects import CvContentType, ExtractedText
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.admin_guest_world import GUESTS, UNMATCHED, Guest, Who, World, open_world
from tests.api.me_support import Entry, error_body, error_code, export_bytes
from tests.integration.owners import queued_export

CV_KEYS_PLUS_TEXT = "extracted_text"
SUMMARY_KEYS = {
    "id",
    "created_at",
    "expires_at",
    "base_cvs",
    "job_postings",
    "tailoring_runs",
    "export_jobs",
}
ITEM_404 = {
    "get_cv": "base_cv_not_found",
    "get_posting": "job_posting_not_found",
    "get_run": "tailoring_run_not_found",
    "download": "tailoring_run_not_found",
    "list_exports": "tailoring_run_not_found",
    "export_file": "export_job_not_found",
}
READ_ROUTES = ["get_cv", "get_posting", "get_run", "download", "list_exports", "export_file"]
SESSION_ROUTES = ["overview", *READ_ROUTES]


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """The admin routes take no limiter; a login in `World.account` does."""


@pytest_asyncio.fixture
async def world(
    settings: Settings,
    engine: AsyncEngine,
    password_hasher: Argon2PasswordHasher,
    tmp_path: Path,
) -> AsyncIterator[World]:
    async with open_world(settings, engine, password_hasher, tmp_path / "uploads") as w:
        yield w


# ----------------------------------------------------------------------------- helpers


def _no_store(response: Response) -> None:
    assert response.headers.get("cache-control") == "no-store", dict(response.headers)


def _comparable(response: Response) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k != "date"}


def _real(path: Path) -> str:
    return os.path.realpath(path)


def _exists(path: Path) -> bool:
    return path.exists()


def _routes(
    sid: object, cv: object, posting: object, run: object, job: object
) -> dict[str, tuple[str, str]]:
    """The nine routes as (method, url), by name."""
    s = f"{GUESTS}/{sid}"
    return {
        "list": ("GET", GUESTS),
        "overview": ("GET", s),
        "erase": ("DELETE", s),
        "get_cv": ("GET", f"{s}/base-cvs/{cv}"),
        "get_posting": ("GET", f"{s}/job-postings/{posting}"),
        "get_run": ("GET", f"{s}/tailoring-runs/{run}"),
        "download": ("GET", f"{s}/tailoring-runs/{run}/documents/cv/download?format=md"),
        "list_exports": ("GET", f"{s}/tailoring-runs/{run}/exports"),
        "export_file": ("GET", f"{s}/export-jobs/{job}/file"),
    }


def _entry_routes(guest: Guest, entry: Entry) -> dict[str, tuple[str, str]]:
    return _routes(
        guest.uuid,
        entry.cv_id.value,
        entry.posting_id.value,
        entry.run_id.value,
        entry.jobs[0].id.value,
    )


def _random_routes(sid: object) -> dict[str, tuple[str, str]]:
    return _routes(sid, uuid4(), uuid4(), uuid4(), uuid4())


async def _all_pages(world: World, admin: Who, *, limit: int = 2) -> list[dict[str, Any]]:
    """Walk every page of the directory; the test database may hold sessions of other tests."""
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(500):
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = await world.http.get(GUESTS, params=params, headers=admin.headers)
        assert response.status_code == 200, response.text
        body = response.json()
        items += body["items"]
        cursor = body["next_cursor"]
        if cursor is None:
            return items
    raise AssertionError("the directory never ended")


# ----------------------------------------------------------------------------- AC-9 the list


async def test_ac9_the_list_has_the_seven_summary_keys_exact_counts_and_no_store(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    await world.entry(guest.owner, minutes_ago=20)
    await world.entry(guest.owner, minutes_ago=10)
    await world.cv(guest.owner, minutes_ago=5)

    response = await world.http.get(GUESTS, params={"limit": 50}, headers=admin.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    assert set(response.json()) == {"items", "next_cursor"}
    mine = [i for i in response.json()["items"] if i["id"] == str(guest.uuid)]
    assert len(mine) == 1, "the live session is listed once"
    item = mine[0]
    assert set(item) == SUMMARY_KEYS
    assert (
        item["base_cvs"],
        item["job_postings"],
        item["tailoring_runs"],
        item["export_jobs"],
    ) == (
        3,
        2,
        2,
        2,
    )
    assert datetime.fromisoformat(item["created_at"]) == guest.created_at
    assert datetime.fromisoformat(item["expires_at"]) == guest.expires_at


async def test_ac9_nothing_that_could_authenticate_as_the_guest_is_ever_returned(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner)

    bodies: list[str] = []
    for method, url in _entry_routes(guest, entry).values():
        if method == "DELETE":
            continue
        response = await world.http.request(method, url, headers=admin.headers)
        assert response.status_code == 200, f"{url}: {response.text}"
        bodies.append(response.text)
    listing = await world.http.get(GUESTS, params={"limit": 50}, headers=admin.headers)

    assert str(guest.uuid) in listing.text, "positive control: the session is in the list"
    for text in [*bodies, listing.text]:
        assert guest.token not in text
        assert guest.token_hash not in text
        assert "token_hash" not in text


async def test_ac9_an_expired_unpurged_session_is_absent_and_a_live_one_is_present(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    live = await world.guest()
    expired = await world.guest(started=timedelta(hours=30))

    ids = [i["id"] for i in await _all_pages(world, admin)]

    assert str(live.uuid) in ids
    assert str(expired.uuid) not in ids


async def test_ac9_pages_are_latest_expiry_first_and_never_repeat_or_skip_a_session(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    tied_at = SystemClock().now() - timedelta(minutes=7)
    tied = [await world.guest(at=tied_at) for _ in range(3)]  # equal expires_at
    older = await world.guest(started=timedelta(minutes=30))
    newer = await world.guest(started=timedelta(seconds=5))
    mine = [*tied, older, newer]

    items = await _all_pages(world, admin, limit=1)  # every boundary is a keyset boundary

    ids = [UUID(i["id"]) for i in items]
    assert len(ids) == len(set(ids)), "a session was repeated across pages"
    keys = [(datetime.fromisoformat(i["expires_at"]), UUID(i["id"])) for i in items]
    assert keys == sorted(keys, reverse=True), "not latest expiry first (id descending on a tie)"
    wanted = sorted(((g.expires_at, g.uuid) for g in mine), reverse=True)
    assert [k for k in keys if k[1] in {g.uuid for g in mine}] == wanted, "a session was skipped"


async def test_ac9_a_page_holds_at_most_limit_and_says_when_there_is_more(world: World) -> None:
    admin = await world.account(admin=True)
    await world.guest()
    await world.guest(started=timedelta(minutes=2))

    response = await world.http.get(GUESTS, params={"limit": 1}, headers=admin.headers)

    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1
    assert isinstance(response.json()["next_cursor"], str)


async def test_ac9_the_default_page_size_is_20(world: World) -> None:
    admin = await world.account(admin=True)
    for n in range(21):
        await world.guest(started=timedelta(seconds=n + 1))

    response = await world.http.get(GUESTS, headers=admin.headers)

    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 20
    assert response.json()["next_cursor"] is not None


async def test_ac9_no_live_session_is_an_empty_page_not_a_404(
    settings: Settings,
    engine: AsyncEngine,
    password_hasher: Argon2PasswordHasher,
    tmp_path: Path,
) -> None:
    async with open_world(
        settings, engine, password_hasher, tmp_path / "uploads", clean_guest_sessions=True
    ) as world:
        admin = await world.account(admin=True)
        await world.guest(started=timedelta(hours=30))  # an expired one does not count

        response = await world.http.get(GUESTS, headers=admin.headers)

        assert response.status_code == 200, response.text
        assert response.json() == {"items": [], "next_cursor": None}
        _no_store(response)


# ----------------------------------------------------------------------------- AC-10 cursor, limit


@pytest.mark.parametrize("bad", ["!!!not-base64", "A" * 40, "e30"])
async def test_ac10_a_malformed_cursor_is_422_invalid_cursor_and_never_echoed(
    world: World, bad: str
) -> None:
    admin = await world.account(admin=True)

    response = await world.http.get(GUESTS, params={"cursor": bad}, headers=admin.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "invalid_cursor"
    assert bad not in response.text
    _no_store(response)


async def test_ac10_a_tampered_cursor_is_422_invalid_cursor(world: World) -> None:
    admin = await world.account(admin=True)
    await world.guest()
    await world.guest(started=timedelta(minutes=2))
    first = await world.http.get(GUESTS, params={"limit": 1}, headers=admin.headers)
    assert first.status_code == 200, first.text
    cursor = first.json()["next_cursor"]
    assert isinstance(cursor, str), "premise: a real cursor to tamper with"
    tampered = cursor[:-4] + "!!!!"

    response = await world.http.get(GUESTS, params={"cursor": tampered}, headers=admin.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "invalid_cursor"
    assert tampered not in response.text


@pytest.mark.parametrize("limit", ["0", "51", "-1", "x", ""])
async def test_ac10_a_limit_outside_1_to_50_is_422_validation_error(
    world: World, limit: str
) -> None:
    admin = await world.account(admin=True)

    response = await world.http.get(GUESTS, params={"limit": limit}, headers=admin.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"


async def test_ac10_a_cursor_over_the_raw_bound_is_422_validation_error(world: World) -> None:
    admin = await world.account(admin=True)

    response = await world.http.get(GUESTS, params={"cursor": "a" * 300}, headers=admin.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"


# ----------------------------------------------------------------------------- AC-11 the overview


async def test_ac11_the_overview_is_the_summary_plus_the_guests_own_list_shapes(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    await world.entry(guest.owner, minutes_ago=20)
    await world.entry(guest.owner, minutes_ago=10)
    await world.cv(guest.owner, minutes_ago=5)
    own = world.guest_client(guest)

    response = await world.http.get(f"{GUESTS}/{guest.uuid}", headers=admin.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    body = response.json()
    assert set(body) == {"session", "base_cvs", "job_postings", "tailoring_runs"}
    assert set(body["session"]) == SUMMARY_KEYS
    assert body["session"]["id"] == str(guest.uuid)
    assert (body["session"]["base_cvs"], body["session"]["export_jobs"]) == (3, 2)
    for key, path in [
        ("base_cvs", "/api/base-cvs"),
        ("job_postings", "/api/job-postings"),
        ("tailoring_runs", "/api/tailoring-runs"),
    ]:
        theirs = await own.get(path)
        assert theirs.status_code == 200, f"{path}: {theirs.text}"
        assert body[key] == theirs.json()["items"], f"{key}: not the guest's own list"
        assert len(body[key]) > 0, f"{key}: premise, a non-empty list"


async def test_ac11_the_administrators_own_guest_cookie_is_never_the_subject(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    path_session = await world.guest()
    cookie_session = await world.guest()
    await world.cv(path_session.owner)
    carrying = world.guest_client(cookie_session)

    response = await carrying.get(f"{GUESTS}/{path_session.uuid}", headers=admin.headers)

    assert response.status_code == 200, response.text
    assert response.json()["session"]["id"] == str(path_session.uuid)
    assert len(response.json()["base_cvs"]) == 1
    assert "set-cookie" not in response.headers, "a guest session is never minted or refreshed"


@pytest.mark.parametrize("state", ["unknown", "expired"])
async def test_ac11_an_unknown_or_expired_session_is_404_guest_session_not_found_never_401(
    world: World, state: str
) -> None:
    admin = await world.account(admin=True)
    sid = uuid4() if state == "unknown" else (await world.guest(started=timedelta(hours=30))).uuid

    response = await world.http.get(f"{GUESTS}/{sid}", headers=admin.headers)

    assert response.status_code == 404, response.text
    assert error_code(response) == "guest_session_not_found"
    _no_store(response)


async def test_ac11_a_malformed_session_id_is_422_validation_error(world: World) -> None:
    admin = await world.account(admin=True)

    response = await world.http.get(f"{GUESTS}/not-a-uuid", headers=admin.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"


# ----------------------------------------------------------------------------- AC-12 the erase


async def test_ac12_erase_removes_the_session_its_rows_and_its_files_and_nothing_else(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    target, other = await world.guest(), await world.guest()
    user = await world.account()
    t_entry = await world.entry(target.owner)
    t_cv = await world.cv(target.owner)
    o_entry = await world.entry(other.owner)
    u_entry = await world.entry(user.owner)
    target_files = [
        world.path(_cv_ref(t_entry)),
        world.path(t_entry.jobs[0].storage_ref),
        world.path(t_cv.file),
    ]
    kept_files = [
        world.path(o_entry.jobs[0].storage_ref),
        world.path(u_entry.jobs[0].storage_ref),
        world.path(_cv_ref(o_entry)),
        world.path(_cv_ref(u_entry)),
    ]
    assert all(_exists(p) for p in [*target_files, *kept_files]), "premise: files on the volume"
    other_rows, user_rows = await world.rows_of_guest(other), await world.rows_of_user(user)

    response = await world.http.delete(f"{GUESTS}/{target.uuid}", headers=admin.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    # the session, its CV with its own file (3 files: cv, second cv, pdf export) and its rows
    assert response.json()["unlink_failures"] == 0
    assert response.json()["files_unlinked"] == 3
    assert set(response.json()) == {"files_unlinked", "unlink_failures"}
    assert set((await world.rows_of_guest(target)).values()) == {0}
    assert not any(_exists(p) for p in target_files)
    assert all(_exists(p) for p in kept_files), "another session's and a user's files are kept"
    assert await world.rows_of_guest(other) == other_rows
    assert await world.rows_of_user(user) == user_rows


async def test_ac12_a_failing_unlink_is_still_200_counted_and_the_rows_are_gone(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await world.account(admin=True)
    target = await world.guest()
    entry = await world.entry(target.owner)
    failing = _real(world.upload_dir / entry.jobs[0].storage_ref.key)
    original = os.unlink

    def _selective(path: Any, *args: Any, **kwargs: Any) -> None:
        if os.path.realpath(os.fspath(path)) == failing:
            raise OSError(5, "Input/output error")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", _selective)

    response = await world.http.delete(f"{GUESTS}/{target.uuid}", headers=admin.headers)

    assert response.status_code == 200, response.text
    assert response.json()["unlink_failures"] == 1
    assert response.json()["files_unlinked"] == 1
    assert set((await world.rows_of_guest(target)).values()) == {0}
    assert _exists(Path(failing)), "the failing unlink left its bytes for the orphan sweep"


async def test_ac12_an_expired_session_can_be_erased_early_by_the_same_step(world: World) -> None:
    admin = await world.account(admin=True)
    expired = await world.guest(started=timedelta(hours=30))
    await world.cv(expired.owner)

    response = await world.http.delete(f"{GUESTS}/{expired.uuid}", headers=admin.headers)

    assert response.status_code == 200, response.text
    assert response.json()["files_unlinked"] == 1
    assert set((await world.rows_of_guest(expired)).values()) == {0}


async def test_ac12_a_second_erase_and_an_unknown_id_are_404_guest_session_not_found(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    target = await world.guest()
    first = await world.http.delete(f"{GUESTS}/{target.uuid}", headers=admin.headers)
    assert first.status_code == 200, first.text

    again = await world.http.delete(f"{GUESTS}/{target.uuid}", headers=admin.headers)
    unknown = await world.http.delete(f"{GUESTS}/{uuid4()}", headers=admin.headers)

    for response in (again, unknown):
        assert response.status_code == 404, response.text
        assert error_code(response) == "guest_session_not_found"
        _no_store(response)


# ----------------------------------------------------------------------------- AC-13 the twins


async def test_ac13_one_cv_is_the_guests_own_response_plus_extracted_text(world: World) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner)
    own = world.guest_client(guest)

    response = await world.http.get(
        f"{GUESTS}/{guest.uuid}/base-cvs/{entry.cv_id.value}", headers=admin.headers
    )
    theirs = await own.get(f"/api/base-cvs/{entry.cv_id.value}")

    assert response.status_code == 200, response.text
    assert theirs.status_code == 200, theirs.text
    _no_store(response)
    body = response.json()
    assert set(body) == set(theirs.json()) | {CV_KEYS_PLUS_TEXT}
    assert body[CV_KEYS_PLUS_TEXT] == ExtractedText("word " * 60).value
    assert {k: v for k, v in body.items() if k != CV_KEYS_PLUS_TEXT} == theirs.json()


async def test_ac13_one_posting_is_the_guests_own_response(world: World) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner)
    own = world.guest_client(guest)

    response = await world.http.get(
        f"{GUESTS}/{guest.uuid}/job-postings/{entry.posting_id.value}", headers=admin.headers
    )
    theirs = await own.get(f"/api/job-postings/{entry.posting_id.value}")

    assert response.status_code == 200, response.text
    assert theirs.status_code == 200, theirs.text
    _no_store(response)
    assert response.json() == theirs.json()


async def test_ac13_one_run_is_the_guests_own_response(world: World) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner)
    own = world.guest_client(guest)

    response = await world.http.get(
        f"{GUESTS}/{guest.uuid}/tailoring-runs/{entry.run_id.value}", headers=admin.headers
    )
    theirs = await own.get(f"/api/tailoring-runs/{entry.run_id.value}")

    assert response.status_code == 200, response.text
    assert theirs.status_code == 200, theirs.text
    _no_store(response)
    assert response.json() == theirs.json()


@pytest.mark.parametrize("fmt", ["md", "txt"])
async def test_ac13_a_document_download_is_the_guests_bytes_and_three_headers(
    world: World, fmt: str
) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner)
    own = world.guest_client(guest)
    tail = f"tailoring-runs/{entry.run_id.value}/documents/cover_letter/download"

    response = await world.http.get(
        f"{GUESTS}/{guest.uuid}/{tail}", params={"format": fmt}, headers=admin.headers
    )
    theirs = await own.get(f"/api/{tail}", params={"format": fmt})

    assert response.status_code == 200, response.text
    assert theirs.status_code == 200, theirs.text
    assert response.content == theirs.content
    assert len(response.content) > 0
    for header in (
        "cache-control",
        "x-content-type-options",
        "content-disposition",
        "content-type",
    ):
        assert response.headers.get(header) == theirs.headers.get(header), header
    assert response.headers["cache-control"] == "no-store"


async def test_ac13_the_exports_list_is_the_guests_with_the_file_url_under_the_admin_path(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner)
    own = world.guest_client(guest)
    job = entry.jobs[0].id.value

    response = await world.http.get(
        f"{GUESTS}/{guest.uuid}/tailoring-runs/{entry.run_id.value}/exports",
        headers=admin.headers,
    )
    theirs = await own.get(f"/api/tailoring-runs/{entry.run_id.value}/exports")

    assert response.status_code == 200, response.text
    assert theirs.status_code == 200, theirs.text
    _no_store(response)
    items, their_items = response.json()["items"], theirs.json()["items"]
    assert len(items) == len(their_items) == 1
    assert items[0]["file_url"] == f"{GUESTS}/{guest.uuid}/export-jobs/{job}/file"
    assert their_items[0]["file_url"] == f"/api/export-jobs/{job}/file"
    assert {**items[0], "file_url": None} == {**their_items[0], "file_url": None}


async def test_ac13_an_export_file_is_the_guests_bytes_and_three_headers(world: World) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner)
    own = world.guest_client(guest)
    job = entry.jobs[0].id

    response = await world.http.get(
        f"{GUESTS}/{guest.uuid}/export-jobs/{job.value}/file", headers=admin.headers
    )
    theirs = await own.get(f"/api/export-jobs/{job.value}/file")

    assert response.status_code == 200, response.text
    assert theirs.status_code == 200, theirs.text
    assert response.content == export_bytes(job) == theirs.content
    for header in (
        "cache-control",
        "x-content-type-options",
        "content-disposition",
        "content-type",
    ):
        assert response.headers.get(header) == theirs.headers.get(header), header
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("state", ["queued", "failed"])
async def test_ac13_a_job_that_is_not_ready_is_409_export_not_ready(
    world: World, state: str
) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner, ready_formats=())
    now = SystemClock().now()
    job = queued_export(
        guest.owner, entry.run, now - timedelta(minutes=5), format=ExportFormat.DOCX
    )
    if state == "failed":
        job.mark_started(now - timedelta(minutes=4))
        job.mark_failed(ExportFailureReason.RENDER_FAILED, now - timedelta(minutes=3))
        job.release_events()
    async with world.app.state.session_factory() as session:
        await SqlAlchemyExportJobRepository(session).add(job)
        await session.commit()

    response = await world.http.get(
        f"{GUESTS}/{guest.uuid}/export-jobs/{job.id.value}/file", headers=admin.headers
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "export_not_ready"
    assert error_body(response)["status"] == state
    _no_store(response)


async def test_ac13_a_ready_row_whose_file_is_gone_is_410_export_file_gone(world: World) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner)
    (world.upload_dir / entry.jobs[0].storage_ref.key).unlink()

    response = await world.http.get(
        f"{GUESTS}/{guest.uuid}/export-jobs/{entry.jobs[0].id.value}/file", headers=admin.headers
    )

    assert response.status_code == 410, response.text
    assert error_code(response) == "export_file_gone"
    _no_store(response)


async def test_ac13_a_failing_file_store_is_503_storage_unavailable(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injected below `LocalFileStore.get`'s own `except OSError` floor, at its synchronous read."""
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner)

    def _eio(self: LocalFileStore, ref: object) -> bytes:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(LocalFileStore, "_get_sync", _eio)

    response = await world.http.get(
        f"{GUESTS}/{guest.uuid}/export-jobs/{entry.jobs[0].id.value}/file", headers=admin.headers
    )

    assert response.status_code == 503, response.text
    assert error_code(response) == "storage_unavailable"


# ----------------------------------------------------------------------------- AC-14 equality


async def test_ac14_another_sessions_or_a_users_item_is_that_kinds_404_and_reveals_nothing(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    mine, theirs = await world.guest(), await world.guest()
    user = await world.account()
    own_entry = await world.entry(mine.owner)
    foreign = {
        "another session": await world.entry(theirs.owner),
        "a user": await world.entry(user.owner),
    }
    url = _entry_routes(mine, own_entry)
    for name in READ_ROUTES:  # positive control: the same routes answer 200 for the session's own
        response = await world.http.request(url[name][0], url[name][1], headers=admin.headers)
        assert response.status_code == 200, f"{name}: {response.text}"

    for owner_of_item, other in foreign.items():
        for name, (method, route) in _entry_routes(mine, other).items():
            if name not in READ_ROUTES:
                continue
            response = await world.http.request(method, route, headers=admin.headers)
            assert response.status_code == 404, f"{owner_of_item} {name}: {response.text}"
            assert error_code(response) == ITEM_404[name], f"{owner_of_item} {name}"
            _no_store(response)
            assert str(theirs.uuid) not in response.text
            assert str(user.id) not in response.text


@pytest.mark.parametrize("how", ["expired", "erased"])
async def test_ac14_a_session_gone_mid_browse_is_404_guest_session_not_found_on_every_guest_route_never_401(
    world: World, how: str
) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner)
    routes = _entry_routes(guest, entry)
    for name in SESSION_ROUTES:  # positive control: all answered 200 while it was live
        response = await world.http.request(routes[name][0], routes[name][1], headers=admin.headers)
        assert response.status_code == 200, f"{name}: {response.text}"
    if how == "expired":
        await world.expire(guest)
    else:
        erased = await world.http.delete(f"{GUESTS}/{guest.uuid}", headers=admin.headers)
        assert erased.status_code == 200, erased.text

    for name in SESSION_ROUTES:
        response = await world.http.request(routes[name][0], routes[name][1], headers=admin.headers)
        assert response.status_code == 404, f"{name}: {response.status_code} {response.text}"
        assert error_code(response) == "guest_session_not_found", name
        _no_store(response)

    still_admin = await world.http.get(GUESTS, headers=admin.headers)
    assert still_admin.status_code == 200, "the administrator was not signed out"


# ----------------------------------------------------------------------------- AC-23 firewall


def _firewalled(sid: object) -> list[tuple[str, str]]:
    """Every route, valid-shaped and malformed in every parameter."""
    cv, post, run, job = uuid4(), uuid4(), uuid4(), uuid4()
    urls = list(_routes(sid, cv, post, run, job).values())
    urls += list(_routes("not-a-uuid", cv, post, run, job).values())
    urls += list(_routes(sid, "not-a-uuid", "not-a-uuid", "not-a-uuid", "not-a-uuid").values())
    s = f"{GUESTS}/{sid}"
    urls += [
        ("GET", f"{GUESTS}?cursor=!!&limit=999"),
        ("GET", f"{GUESTS}?cursor={'a' * 300}"),
        ("GET", f"{GUESTS}?limit=0"),
        ("GET", f"{GUESTS}?limit=51"),
        ("GET", f"{s}/tailoring-runs/{run}/documents/cv/download?format=pdf"),
        ("GET", f"{s}/tailoring-runs/{run}/documents/cv/download"),
        ("GET", f"{s}/tailoring-runs/{run}/documents/resume/download?format=md"),
    ]
    return urls


async def test_ac23_a_plain_user_gets_the_404_of_an_unmatched_path_and_nothing_runs(
    world: World,
) -> None:
    user = await world.account()
    guest = await world.guest()
    entry = await world.entry(guest.owner)
    before = await world.rows_of_guest(guest)
    files = [world.upload_dir / entry.jobs[0].storage_ref.key]
    unmatched = await world.http.get(UNMATCHED, headers=user.headers)
    assert unmatched.status_code == 404, unmatched.text

    for method, url in [*_firewalled(guest.uuid), *_entry_routes(guest, entry).values()]:
        refused = await world.http.request(method, url, headers=user.headers)
        assert refused.status_code == 404, f"{method} {url}: {refused.text}"
        assert refused.content == unmatched.content, f"{method} {url}"
        assert refused.json() == {"detail": "Not Found"}
        assert _comparable(refused) == _comparable(unmatched), f"{method} {url}"

    assert await world.rows_of_guest(guest) == before, "a refused DELETE changed a row"
    assert all(_exists(p) for p in files), "a refused DELETE removed a file"


async def test_ac23_anonymous_is_401_invalid_access_token_on_all_nine(world: World) -> None:
    for name, (method, url) in _random_routes(uuid4()).items():
        response = await world.http.request(method, url)
        assert response.status_code == 401, f"{name}: {response.text}"
        assert error_code(response) == "invalid_access_token", name


async def test_ac23_a_disabled_admin_is_403_account_disabled_on_all_nine(world: World) -> None:
    disabled, other = await world.account(admin=True), await world.account(admin=True)
    done = await world.http.post(f"/api/admin/users/{disabled.id}/disable", headers=other.headers)
    assert done.status_code == 200, done.text

    for name, (method, url) in _random_routes(uuid4()).items():
        response = await world.http.request(method, url, headers=disabled.headers)
        assert response.status_code == 403, f"{name}: {response.text}"
        assert error_code(response) == "account_disabled", name


# ----------------------------------------------------------------------------- local helpers


def _cv_ref(entry: Entry) -> FileRef:
    return FileRef.for_base_cv(entry.cv_id, CvContentType.PDF)
