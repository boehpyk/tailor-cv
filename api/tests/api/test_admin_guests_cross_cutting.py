"""Slice 4.4, T16 (test-after): AC-24 ... AC-26, AC-29 and AC-30 over the fourteen admin guest and
retention routes (`admin_guests.py`, `admin_retention.py`).

* AC-24: Postgres failing at the **sync** `Session.execute` (4.3's `f5b461f`, which both
  `AsyncSession.execute` and `AsyncSession.get` reach) is a 503 `service_unavailable` on each read
  route, never a 404 or a 200; an erase whose commit fails is a 503 with every row and file intact.
* AC-25: `no-store` on every response a handler builds, over the routes x their refusals.
* AC-26: no guest cookie is read or minted.
* AC-29: the two admin lines' fields, and the CLI's `retention.*` lines **field for field**. The CLI's
  key sets are written out below from `purge_command._log_purge_line` / `_reclaim_orphans`; a key the
  admin route added or dropped turns a test red.
* AC-30: planted markers over a full walk; ids are the positive control.

The world is 4.4's (`admin_guest_world`): a concurrent app, a per-test volume, a clean guest table.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI
from httpx import Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session

from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId
from tailorcraft.domain.identity.ownership import Owner, UserOwner
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
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.observability import configure_logging
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
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.retention.lock import PURGE_LOCK_KEY
from tailorcraft.infrastructure.settings import Settings
from tests.api.admin_guest_world import GUESTS, Guest, World, open_world
from tests.api.me_support import Entry, error_code, export_bytes
from tests.api.test_admin_guests import READ_ROUTES, _entry_routes, _random_routes
from tests.api.test_auth_route_dependency_boundary import (
    test_ac32_transfer_route_exception_set_is_exactly_the_claim_route as _transfer_set,
)
from tests.integration.owners import a_draft, queued_export, queued_run, ready_export, running_run

STORAGE = "/api/admin/storage"
PURGE = "/api/admin/guest-purge"
SWEEP = "/api/admin/orphan-sweep"
CONTENT_EVENT = "admin.content_access"
RETENTION_EVENT = "admin.retention_action"
_META = {"event", "level", "timestamp", "logger", "logger_name", "filename", "lineno", "func_name"}

# The CLI's lines, field for field (purge_command._log_purge_line, _reclaim_orphans).
CLI_PURGE_COMPLETED = {
    "sessions_deleted",
    "sessions_skipped",
    "sessions_failed",
    "files_unlinked",
    "files_failed",
    "examined",
    "overdue_after",
    "dry_run",
    "duration_ms",
}
CLI_ORPHANS_COMPLETED = {
    "scanned",
    "referenced",
    "too_young",
    "unrecognized",
    "reclaimed",
    "failed",
    "dry_run",
    "duration_ms",
}
CLI_ERROR_LINE = {"error_type", "duration_ms"}

_TABLES = (
    "identity_guest_session",
    "intake_base_cv",
    "posting_job_posting",
    "tailoring_run",
    "export_job",
)


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """The purge lock and heartbeat live in Redis, which the database rollback does not reach."""


@pytest.fixture(autouse=True)
def _configured_logging(settings: Settings) -> None:
    configure_logging(settings)


@pytest_asyncio.fixture
async def world(
    settings: Settings,
    engine: AsyncEngine,
    password_hasher: Argon2PasswordHasher,
    tmp_path: Path,
) -> AsyncIterator[World]:
    async with open_world(
        settings, engine, password_hasher, tmp_path / "uploads", clean_guest_sessions=True
    ) as w:
        yield w


@pytest_asyncio.fixture
async def redis(settings: Settings) -> AsyncIterator[aioredis.Redis]:
    client = create_redis(settings.redis_url)
    yield client
    await client.aclose()


def _real(path: Path) -> str:
    return os.path.realpath(path)


def _no_store(response: Response) -> None:
    assert response.headers.get("cache-control") == "no-store", dict(response.headers)


def _lines(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for record in caplog.records:
        message = record.getMessage()
        if event not in message:
            continue
        try:
            parsed = json.loads(message)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("event") == event:
            found.append(parsed)
    return found


def _fields(line: dict[str, Any]) -> set[str]:
    return set(line) - _META


def _all_routes(guest: Guest, entry: Entry) -> dict[str, tuple[str, str]]:
    return {
        **_entry_routes(guest, entry),
        "storage": ("GET", STORAGE),
        "purge_preview": ("GET", PURGE),
        "purge_run": ("POST", PURGE),
        "sweep_preview": ("GET", SWEEP),
        "sweep_run": ("POST", SWEEP),
    }


def _body(name: str) -> dict[str, Any] | None:
    bodies: dict[str, dict[str, Any]] = {"purge_run": {"limit": 100}, "sweep_run": {"limit": None}}
    return bodies.get(name)


# ----------------------------------------------------------------------------- AC-24


@pytest.mark.parametrize("route", ["list", "overview", *READ_ROUTES, "storage"])
async def test_ac24_postgres_failing_below_the_repository_is_503_never_404_or_200(
    world: World, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """Fails any statement that touches a guest table (or asks which file keys rows name), so the
    handler's own read is what breaks; the bearer check (`identity_user`) still works."""
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner)
    method, url = _all_routes(guest, entry)[route]
    original = Session.execute

    def _down(self: Session, statement: Any, *args: Any, **kwargs: Any) -> Any:
        sql = str(statement)
        if any(table in sql for table in _TABLES) or "file_key" in sql:
            raise SQLAlchemyError("simulated: postgres unavailable (AC-24)")
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", _down)

    response = await world.http.request(method, url, headers=admin.headers)

    assert response.status_code == 503, response.text
    # The global 503 carries no `no-store` (4.3's accepted gap): it holds no account data.
    assert error_code(response) == "service_unavailable"


async def test_ac24_an_erase_whose_commit_fails_is_503_with_every_row_and_file_intact(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner)
    files = [world.path(entry.jobs[0].storage_ref)]
    before = await world.rows_of_guest(guest)
    assert all(f.exists() for f in files), "premise: the files are on the volume"
    assert set(before.values()) != {0}, "premise: the rows exist"

    async def _commit_fails(self: AsyncSession) -> None:
        raise SQLAlchemyError("simulated commit failure (AC-24)")

    monkeypatch.setattr(AsyncSession, "commit", _commit_fails)

    response = await world.http.delete(f"{GUESTS}/{guest.uuid}", headers=admin.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "service_unavailable"
    _no_store(response)
    monkeypatch.undo()
    assert await world.rows_of_guest(guest) == before
    assert all(f.exists() for f in files), "no file is touched when the DELETE did not commit"


# ----------------------------------------------------------------------------- AC-25


async def test_ac25_every_response_a_handler_builds_carries_no_store_success_or_refusal(
    world: World, redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner)
    now = SystemClock().now()
    busy = await world.entry(
        guest.owner,
        minutes_ago=20,
        run=queued_run(guest.owner, now - timedelta(minutes=20)),
        ready_formats=(),
    )
    queued_job = queued_export(
        guest.owner, busy.run, now - timedelta(minutes=5), format=ExportFormat.DOCX
    )
    async with world.app.state.session_factory() as session:
        await SqlAlchemyExportJobRepository(session).add(queued_job)
        await session.commit()
    gone = await world.entry(guest.owner, minutes_ago=30)
    world.path(gone.jobs[0].storage_ref).unlink()
    p = f"{GUESTS}/{guest.uuid}"

    requests: list[tuple[str, str, str, dict[str, Any] | None]] = []
    for name, (method, url) in _random_routes(uuid4()).items():
        requests.append((f"unknown-session {name}", method, url, None))
    for name in READ_ROUTES:
        method, url = _random_routes(guest.uuid)[name]
        requests.append((f"unknown-item {name}", method, url, None))
    requests += [
        (
            "not-exportable",
            "GET",
            f"{p}/tailoring-runs/{busy.run_id.value}/documents/cv/download?format=md",
            None,
        ),
        ("export-not-ready", "GET", f"{p}/export-jobs/{queued_job.id.value}/file", None),
        ("file-gone", "GET", f"{p}/export-jobs/{gone.jobs[0].id.value}/file", None),
        ("bad-cursor", "GET", f"{GUESTS}?cursor=not-a-cursor", None),
    ]

    def _eio(self: LocalFileStore, ref: object) -> bytes:
        raise OSError(5, "Input/output error")

    with monkeypatch.context() as patched:
        patched.setattr(LocalFileStore, "_get_sync", _eio)
        unavailable = await world.http.get(
            f"{p}/export-jobs/{entry.jobs[0].id.value}/file", headers=admin.headers
        )
        assert unavailable.status_code == 503, unavailable.text
        _no_store(unavailable)

    for label, method, url, body in requests:
        response = await world.http.request(method, url, json=body, headers=admin.headers)
        assert response.status_code != 500, f"{label}: {response.text}"
        _no_store(response)

    # 409 purge_in_progress: a held lock.
    assert await redis.set(PURGE_LOCK_KEY, "foreign", nx=True, px=60_000)
    held = await world.http.post(PURGE, json={"limit": 100}, headers=admin.headers)
    assert held.status_code == 409, held.text
    _no_store(held)
    await redis.delete(PURGE_LOCK_KEY)

    # Successes on every route, JSON and binary; the erase last.
    ok = _all_routes(guest, entry)
    for name in [n for n in ok if n != "erase"] + ["erase"]:
        method, url = ok[name]
        response = await world.http.request(method, url, json=_body(name), headers=admin.headers)
        assert response.status_code == 200, f"{name}: {response.text}"
        _no_store(response)


# ----------------------------------------------------------------------------- AC-26


def test_ac26_the_transfer_route_set_is_still_exactly_the_claim_route(app: FastAPI) -> None:
    _transfer_set(app)


async def test_ac26_every_route_answers_identically_with_and_without_a_guest_cookie(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner)
    routes = _all_routes(guest, entry)
    cookie = {"Cookie": f"__Host-tc_guest={guest.token}"}

    def _stable(name: str, response: Response) -> object:
        body = (
            response.json()
            if response.headers["content-type"].startswith("application/json")
            else response.content
        )
        if name == "storage":
            assert isinstance(body, dict)
            body = {k: v for k, v in body.items() if k != "scanned_at"}
        return body

    for name, (method, url) in routes.items():
        if name == "erase":
            continue
        plain = await world.http.request(method, url, json=_body(name), headers=admin.headers)
        with_cookie = await world.http.request(
            method, url, json=_body(name), headers={**admin.headers, **cookie}
        )
        assert plain.status_code == with_cookie.status_code == 200, f"{name}: {plain.text}"
        assert _stable(name, plain) == _stable(name, with_cookie), name
        assert "set-cookie" not in with_cookie.headers, f"{name}: nothing is minted or cleared"

    erased = await world.http.delete(f"{GUESTS}/{guest.uuid}", headers={**admin.headers, **cookie})
    assert erased.status_code == 200, erased.text
    assert "set-cookie" not in erased.headers


# ----------------------------------------------------------------------------- AC-29

EXPECTED_ACCESS = {
    "get_cv": ("base_cv", "read", "cv"),
    "get_posting": ("job_posting", "read", "posting"),
    "get_run": ("tailoring_run", "read", "run"),
    "download": ("tailoring_run", "download", "run"),
    "list_exports": ("export_job", "list", None),
    "export_file": ("export_job", "download", "job"),
}


async def test_ac29_each_guest_read_writes_exactly_one_access_line_with_its_fields(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    entry = await world.entry(guest.owner)
    ids = {
        "cv": str(entry.cv_id.value),
        "posting": str(entry.posting_id.value),
        "run": str(entry.run_id.value),
        "job": str(entry.jobs[0].id.value),
    }
    routes = _entry_routes(guest, entry)

    for name, (kind, action, item) in EXPECTED_ACCESS.items():
        method, url = routes[name]
        caplog.clear()
        with caplog.at_level(logging.INFO):
            response = await world.http.request(method, url, headers=admin.headers)
        assert response.status_code == 200, f"{name}: {response.text}"
        (line,) = _lines(caplog, CONTENT_EVENT)
        expected = {
            "actor_id": str(admin.id),
            "target_guest_session_id": str(guest.uuid),
            "kind": kind,
            "action": action,
            "outcome": "ok",
            **({"item_id": ids[item]} if item else {}),
        }
        assert {k: line[k] for k in _fields(line)} == expected, name
        assert _lines(caplog, RETENTION_EVENT) == [], f"{name}: a read writes one line, not two"


async def test_ac29_a_refused_guest_read_writes_one_not_found_access_line(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    method, url = _random_routes(guest.uuid)["get_cv"]

    with caplog.at_level(logging.INFO):
        response = await world.http.request(method, url, headers=admin.headers)

    assert response.status_code == 404
    (line,) = _lines(caplog, CONTENT_EVENT)
    assert (line["outcome"], line["target_guest_session_id"]) == ("not_found", str(guest.uuid))


async def _one(
    world: World,
    caplog: pytest.LogCaptureFixture,
    admin: Any,
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
) -> tuple[Response, dict[str, Any]]:
    caplog.clear()
    with caplog.at_level(logging.INFO):
        response = await world.http.request(method, url, json=body, headers=admin.headers)
    (line,) = _lines(caplog, RETENTION_EVENT)
    assert _lines(caplog, CONTENT_EVENT) == [], "a retention route writes no content line"
    assert line["actor_id"] == str(admin.id)
    return response, line


async def test_ac29_each_retention_route_writes_exactly_one_action_line_with_counts_only(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    admin = await world.account(admin=True)
    live = await world.guest()
    await world.entry(live.owner)
    expired = await world.guest(started=timedelta(hours=30))
    await world.entry(expired.owner)
    gone = await world.guest()
    await world.entry(gone.owner)
    purge_counts = {
        "examined",
        "sessions_deleted",
        "sessions_skipped",
        "sessions_failed",
        "files_unlinked",
        "files_failed",
        "overdue",
    }
    sweep_counts = {"scanned", "referenced", "too_young", "unrecognized", "reclaimed", "failed"}

    r, line = await _one(world, caplog, admin, "GET", GUESTS)
    assert r.status_code == 200
    assert (line["action"], line["outcome"]) == ("guest_list", "ok")
    assert _fields(line) == {"actor_id", "action", "outcome", "items"}

    r, line = await _one(world, caplog, admin, "GET", f"{GUESTS}/{live.uuid}")
    assert r.status_code == 200
    assert (line["action"], line["outcome"]) == ("guest_overview", "ok")
    assert _fields(line) == {"actor_id", "action", "outcome", "guest_session_id"}
    assert line["guest_session_id"] == str(live.uuid)

    r, line = await _one(world, caplog, admin, "GET", f"{GUESTS}/{uuid4()}")
    assert r.status_code == 404
    assert (line["action"], line["outcome"]) == ("guest_overview", "not_found")

    r, line = await _one(world, caplog, admin, "GET", STORAGE)
    assert r.status_code == 200
    assert (line["action"], line["outcome"]) == ("storage_overview", "ok")
    assert _fields(line) == {
        "actor_id",
        "action",
        "outcome",
        "guest_files",
        "user_files",
        "unreferenced_files",
        "partial_files",
        "unrecognized_files",
    }

    r, line = await _one(world, caplog, admin, "GET", PURGE)
    assert r.status_code == 200
    assert (line["action"], line["outcome"]) == ("purge_preview", "ok")
    assert _fields(line) == {"actor_id", "action", "outcome"} | purge_counts

    r, line = await _one(world, caplog, admin, "POST", PURGE, {"limit": 100})
    assert r.status_code == 200
    assert (line["action"], line["outcome"]) == ("purge_run", "ok")
    assert _fields(line) == {"actor_id", "action", "outcome"} | purge_counts
    assert line["sessions_deleted"] == 1

    r, line = await _one(world, caplog, admin, "GET", SWEEP)
    assert r.status_code == 200
    assert (line["action"], line["outcome"]) == ("sweep_preview", "ok")
    assert _fields(line) == {"actor_id", "action", "outcome"} | sweep_counts

    r, line = await _one(world, caplog, admin, "POST", SWEEP, {"limit": None})
    assert r.status_code == 200
    assert (line["action"], line["outcome"]) == ("sweep_run", "ok")
    assert _fields(line) == {"actor_id", "action", "outcome"} | sweep_counts

    r, line = await _one(world, caplog, admin, "DELETE", f"{GUESTS}/{gone.uuid}")
    assert r.status_code == 200
    assert (line["action"], line["outcome"]) == ("guest_erase", "ok")
    assert _fields(line) == {
        "actor_id",
        "action",
        "outcome",
        "guest_session_id",
        "files_unlinked",
        "unlink_failures",
    }
    assert line["guest_session_id"] == str(gone.uuid)

    r, line = await _one(world, caplog, admin, "DELETE", f"{GUESTS}/{gone.uuid}")
    assert r.status_code == 404
    assert (line["action"], line["outcome"]) == ("guest_erase", "not_found")


async def test_ac29_a_purge_run_writes_the_clis_completed_line_field_for_field(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    admin = await world.account(admin=True)
    expired = await world.guest(started=timedelta(hours=30))
    await world.entry(expired.owner)

    caplog.clear()
    with caplog.at_level(logging.INFO):
        response = await world.http.post(PURGE, json={"limit": 100}, headers=admin.headers)

    assert response.status_code == 200, response.text
    (line,) = _lines(caplog, "retention.purge_completed")
    assert _fields(line) == CLI_PURGE_COMPLETED
    assert line["dry_run"] is False
    assert (line["sessions_deleted"], line["examined"], line["overdue_after"]) == (1, 1, 0)


async def test_ac29_a_purge_run_logs_a_refused_unlink_with_the_error_type_alone(
    world: World, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await world.account(admin=True)
    expired = await world.guest(started=timedelta(hours=30))
    entry = await world.entry(expired.owner)
    failing = _real(world.path(entry.jobs[0].storage_ref))
    original = os.unlink

    def _selective(path: Any, *args: Any, **kwargs: Any) -> None:
        if os.path.realpath(os.fspath(path)) == failing:
            raise OSError(5, "Input/output error")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", _selective)

    with caplog.at_level(logging.INFO):
        response = await world.http.post(PURGE, json={"limit": 100}, headers=admin.headers)

    assert response.status_code == 200, response.text
    (line,) = _lines(caplog, "retention.file_unlink_failed")
    assert _fields(line) == {"error_type"}
    assert failing not in caplog.text


async def test_ac29_a_held_lock_writes_the_skipped_line_and_a_refused_action_line(
    world: World, redis: aioredis.Redis, caplog: pytest.LogCaptureFixture
) -> None:
    admin = await world.account(admin=True)
    assert await redis.set(PURGE_LOCK_KEY, "foreign", nx=True, px=60_000)

    response, line = await _one(world, caplog, admin, "POST", PURGE, {"limit": 100})

    assert response.status_code == 409
    assert (line["action"], line["outcome"]) == ("purge_run", "refused")
    (skipped,) = _lines(caplog, "retention.purge_skipped")
    assert skipped["reason"] == "lock_held"
    assert _lines(caplog, "retention.purge_completed") == []


async def test_ac29_a_failing_purge_writes_the_clis_failed_line_and_a_failed_action_line(
    world: World, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await world.account(admin=True)
    expired = await world.guest(started=timedelta(hours=30))
    await world.entry(expired.owner)
    original = Session.execute

    def _down(self: Session, statement: Any, *args: Any, **kwargs: Any) -> Any:
        sql = str(statement)
        if (
            "identity_guest_session" in sql
            and "count(" not in sql.lower()
            and not sql.lstrip().upper().startswith("DELETE")
        ):
            raise SQLAlchemyError("simulated: postgres unavailable (AC-29)")
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", _down)

    response, line = await _one(world, caplog, admin, "POST", PURGE, {"limit": 100})

    assert response.status_code == 503
    assert (line["action"], line["outcome"]) == ("purge_run", "failed")
    (failed,) = _lines(caplog, "retention.purge_failed")
    assert _fields(failed) == CLI_ERROR_LINE
    assert failed["error_type"] == "SQLAlchemyError"
    assert _lines(caplog, "retention.purge_completed") == []


@pytest.mark.parametrize(("method", "dry_run"), [("GET", True), ("POST", False)])
async def test_ac29_a_sweep_writes_the_clis_completed_line_field_for_field(
    world: World, caplog: pytest.LogCaptureFixture, method: str, dry_run: bool
) -> None:
    admin = await world.account(admin=True)
    orphan = world.path(await world.orphan())

    caplog.clear()
    with caplog.at_level(logging.INFO):
        response = await world.http.request(
            method, SWEEP, json={"limit": None} if method == "POST" else None, headers=admin.headers
        )

    assert response.status_code == 200, response.text
    (line,) = _lines(caplog, "retention.orphan_scan_completed")
    assert _fields(line) == CLI_ORPHANS_COMPLETED
    assert line["dry_run"] is dry_run
    assert (line["scanned"], line["reclaimed"]) == (1, 1), "a preview counts what it would reclaim"
    assert orphan.exists() is dry_run


async def test_ac29_a_sweep_whose_cross_check_cannot_run_writes_the_clis_aborted_line(
    world: World, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await world.account(admin=True)
    await world.orphan()
    original = Session.execute

    def _down(self: Session, statement: Any, *args: Any, **kwargs: Any) -> Any:
        sql = str(statement)
        if "file_key" in sql and ("intake_base_cv" in sql or "export_job" in sql):
            raise SQLAlchemyError("simulated: postgres unavailable (AC-29)")
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", _down)

    response, line = await _one(world, caplog, admin, "POST", SWEEP, {"limit": None})

    assert response.status_code == 503
    assert error_code(response) == "orphan_check_unavailable"
    assert line["outcome"] == "failed"
    (aborted,) = _lines(caplog, "retention.orphan_scan_aborted")
    assert _fields(aborted) == CLI_ERROR_LINE
    assert _lines(caplog, "retention.orphan_scan_completed") == []


# ----------------------------------------------------------------------------- AC-30


async def _marked_entry_for_any_owner(
    world_app: FastAPI, settings: Settings, owner: Owner, at: datetime, marks: dict[str, str]
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
    if isinstance(owner, UserOwner):  # only a saved (user-owned) CV has a label (I-7)
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


async def test_ac30_no_planted_marker_reaches_a_log_record_over_a_full_walk_but_ids_do(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    admin = await world.account(admin=True)
    tag = uuid4().hex[:12]

    def marks(prefix: str) -> dict[str, str]:
        return {
            k: f"MK{prefix}{k.upper().replace('_', '')}{tag}"
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

    guest_marks, expired_marks, user_marks = marks("G"), marks("E"), marks("U")
    now = SystemClock().now()
    live = await world.guest()
    expired = await world.guest(started=timedelta(hours=30))
    user = await world.account()
    entry, job_id = await _marked_entry_for_any_owner(
        world.app, world.settings, live.owner, now - timedelta(minutes=10), guest_marks
    )
    await _marked_entry_for_any_owner(
        world.app, world.settings, expired.owner, now - timedelta(minutes=10), expired_marks
    )
    await _marked_entry_for_any_owner(
        world.app, world.settings, user.owner, now - timedelta(minutes=10), user_marks
    )
    await world.orphan()
    p = f"{GUESTS}/{live.uuid}"
    run_p = f"{p}/tailoring-runs/{entry.run_id.value}"
    steps: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", GUESTS, None),
        ("GET", p, None),
        ("GET", f"{p}/base-cvs/{entry.cv_id.value}", None),
        ("GET", f"{p}/job-postings/{entry.posting_id.value}", None),
        ("GET", run_p, None),
        ("GET", f"{run_p}/documents/cv/download?format=md", None),
        ("GET", f"{run_p}/documents/cv/download?format=txt", None),
        ("GET", f"{run_p}/exports", None),
        ("GET", f"{p}/export-jobs/{job_id.value}/file", None),
        ("GET", STORAGE, None),
        ("GET", PURGE, None),
        ("POST", PURGE, {"limit": 100}),
        ("GET", SWEEP, None),
        ("POST", SWEEP, {"limit": None}),
        ("DELETE", p, None),
    ]
    bodies: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for method, url, body in steps:
            response = await world.http.request(method, url, json=body, headers=admin.headers)
            assert response.status_code == 200, f"{method} {url}: {response.text}"
            bodies.append(response.text)

    log_text = caplog.text
    every_marker = {**{f"g_{k}": v for k, v in guest_marks.items()}}
    every_marker |= {f"e_{k}": v for k, v in expired_marks.items()}
    every_marker |= {f"u_{k}": v for k, v in user_marks.items()}
    for name, marker in every_marker.items():
        assert marker not in log_text, f"{name} reached a log record"
    served = "\n".join(bodies)
    for name in ("filename", "cv_text", "posting_text", "title", "url", "revision"):
        assert guest_marks[name] in served, f"{name} should be in a response body (reads were real)"
    for identifier in (
        str(admin.id),
        str(live.uuid),
        str(entry.cv_id.value),
        str(entry.run_id.value),
    ):
        assert identifier in log_text, "ids are the positive control"
