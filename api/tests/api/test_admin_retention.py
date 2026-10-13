"""Slice 4.4, T14 RED: the admin retention router's HTTP contract (AC-8, AC-15 ... AC-22, AC-23).

Written from the spec and technical plan §0.3-§0.5, §4 against the T13 skeleton, whose five handlers
raise `NotImplementedError`; `new_client` makes that a real 500, so a red reads `assert 500 == 200`.

* The volume is a **per-test** directory (`World.upload_dir`; the app's settings are overridden to
  it), never the shared `upload_dir`: the storage overview and the sweep walk their whole root.
* The guest-session table is emptied at the start (`clean_guest_sessions=True`, guarded to `*_test`
  databases) so "overdue", "examined" and the batch are about sessions this test created.
* The held lock is a **real** `SET retention:guest_purge:lock <foreign> NX PX` on the test Redis
  (DB 3, flushed by `clear_redis`); the heartbeat and the lock key are read back from Redis itself.
* Faults go **below** the floor they test: a Redis client's `execute_command` (under
  `RedisPurgeLock`'s own `except Exception`), the **sync** `Session.execute` (4.3's `f5b461f`), and
  `os.scandir` (under the scanner's `except OSError`).

Regression guards (green against the skeleton, by construction: the router-level `require_admin`
runs before any handler, and FastAPI validates path/query/body before the handler): AC-23's rows and
the 422 rows of AC-22 for a *non-integer / out-of-range* value. They pin existing behaviour.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import Session

from tailorcraft.application.retention.purge_expired_guest_sessions import PurgeExpiredGuestSessions
from tailorcraft.application.retention.reclaim_orphaned_files import ReclaimOrphanedFiles
from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.intake.value_objects import CvContentType
from tailorcraft.domain.retention.value_objects import RetentionWindow
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.api.deps import (
    ClockDep,
    CommittingExpiredGuestDataDep,
    ExpiredGuestDataDep,
    FileStoreDep,
    OrphanFileScannerDep,
    PurgeFactory,
    RetentionWindowDep,
    SweepFactory,
    get_purge_factory,
    get_sweep_factory,
)
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.files.orphan_scanner import LocalOrphanFileScanner
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
    SqlAlchemyExpiredGuestData,
)
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.retention.data_access import CommittingExpiredGuestDataAdapter
from tailorcraft.infrastructure.retention.heartbeat import HEARTBEAT_KEY, RedisPurgeHeartbeat
from tailorcraft.infrastructure.retention.lock import PURGE_LOCK_KEY
from tailorcraft.infrastructure.settings import Settings
from tests.api.admin_guest_world import CV_BYTES, UNMATCHED, World, open_world
from tests.api.me_support import error_code, export_bytes

STORAGE = "/api/admin/storage"
PURGE = "/api/admin/guest-purge"
SWEEP = "/api/admin/orphan-sweep"

PURGE_KEYS = {
    "examined",
    "sessions_deleted",
    "sessions_skipped",
    "sessions_failed",
    "files_unlinked",
    "files_failed",
    "overdue",
    "dry_run",
    "lock",
}
SWEEP_KEYS = {
    "scanned",
    "referenced",
    "too_young",
    "unrecognized",
    "reclaimed",
    "failed",
    "dry_run",
}
BUCKETS = ("guest", "user", "unreferenced", "partial", "unrecognized")
LONG_AGO = timedelta(days=5)  # older than the sweep's floor (24 h window + 24 h grace)


@pytest.fixture(autouse=True)
def _reset_redis_between_tests(clear_redis: None) -> None:
    """The purge lock and heartbeat live in Redis, which the database rollback does not reach."""


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


# ----------------------------------------------------------------------------- helpers


def _no_store(response: Response) -> None:
    assert response.headers.get("cache-control") == "no-store", dict(response.headers)


def _comparable(response: Response) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k != "date"}


def _real(path: Path) -> str:
    return os.path.realpath(path)


def _exists(path: Path) -> bool:
    return path.exists()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


async def _heartbeat(redis: aioredis.Redis) -> Any:
    return await RedisPurgeHeartbeat(redis).read()


async def _expect_no_purge_state(redis: aioredis.Redis) -> None:
    """Neither the lock nor the heartbeat exists — nothing took one, nothing wrote one."""
    assert await redis.get(PURGE_LOCK_KEY) is None, "a purge lock key exists"
    assert await redis.exists(HEARTBEAT_KEY) == 0, "a heartbeat was written"


class Backlog:
    """Two expired sessions (oldest first: `old` names 2 files, `newer` names 1), one live session
    with a file, and one user's entry — all on the test's own volume."""

    def __init__(self, old: Any, newer: Any, live: Any, user: Any) -> None:
        self.old, self.newer, self.live, self.user = old, newer, live, user
        self.old_files: list[Path] = []
        self.newer_files: list[Path] = []
        self.live_files: list[Path] = []
        self.user_files: list[Path] = []

    def all_files(self) -> list[Path]:
        return [*self.old_files, *self.newer_files, *self.live_files, *self.user_files]


async def _backlog(world: World) -> Backlog:
    old = await world.guest(started=timedelta(hours=60))
    newer = await world.guest(started=timedelta(hours=30))
    live = await world.guest()
    user = await world.account()
    old_entry = await world.entry(old.owner)
    newer_cv = await world.cv(newer.owner)
    live_cv = await world.cv(live.owner)
    user_entry = await world.entry(user.owner)
    b = Backlog(old, newer, live, user)
    b.old_files = [world.path(_cv_ref(old_entry)), world.path(old_entry.jobs[0].storage_ref)]
    b.newer_files = [world.path(newer_cv.file)]
    b.live_files = [world.path(live_cv.file)]
    b.user_files = [world.path(_cv_ref(user_entry)), world.path(user_entry.jobs[0].storage_ref)]
    assert all(_exists(p) for p in b.all_files()), "premise: every seeded file is on the volume"
    return b


def _cv_ref(entry: Any) -> FileRef:
    return FileRef.for_base_cv(entry.cv_id, CvContentType.PDF)


# ----------------------------------------------------------------------------- AC-8 called, not copied


_PURGE_BUILT: list[tuple[PurgeExpiredGuestSessions, int, bool]] = []
_SWEEP_BUILT: list[tuple[ReclaimOrphanedFiles, int | None, bool]] = []


def _spy_purge_factory(
    data: CommittingExpiredGuestDataDep,
    files: FileStoreDep,
    clock: ClockDep,
    window: RetentionWindowDep,
) -> PurgeFactory:
    """The real provider, with the use cases it builds recorded."""
    real = get_purge_factory(data, files, clock, window)

    def build(limit: int, dry_run: bool) -> PurgeExpiredGuestSessions:
        use_case = real(limit, dry_run)
        _PURGE_BUILT.append((use_case, limit, dry_run))
        return use_case

    return build


def _spy_sweep_factory(
    scanner: OrphanFileScannerDep,
    data: ExpiredGuestDataDep,
    files: FileStoreDep,
    clock: ClockDep,
    window: RetentionWindowDep,
) -> SweepFactory:
    real = get_sweep_factory(scanner, data, files, clock, window)

    def build(limit: int | None, dry_run: bool) -> ReclaimOrphanedFiles:
        use_case = real(limit, dry_run)
        _SWEEP_BUILT.append((use_case, limit, dry_run))
        return use_case

    return build


async def test_ac8_the_purge_routes_run_the_cli_use_case_with_only_limit_and_dry_run_differing(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    _PURGE_BUILT.clear()
    world.app.dependency_overrides[get_purge_factory] = _spy_purge_factory

    preview = await world.http.get(PURGE, params={"limit": 7}, headers=admin.headers)
    run = await world.http.post(PURGE, json={"limit": 3}, headers=admin.headers)

    assert preview.status_code == 200, preview.text
    assert run.status_code == 200, run.text
    assert [(limit, dry) for _, limit, dry in _PURGE_BUILT] == [(7, True), (3, False)]
    for use_case, limit, dry_run in _PURGE_BUILT:
        assert type(use_case) is PurgeExpiredGuestSessions
        assert type(use_case._data) is CommittingExpiredGuestDataAdapter
        assert type(use_case._files) is LocalFileStore
        assert use_case._window == RetentionWindow(hours=world.settings.guest_retention_hours)
        assert use_case._batch_limit == limit
        assert use_case._dry_run is dry_run


async def test_ac8_the_sweep_routes_run_the_cli_use_case_with_the_default_grace(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    _SWEEP_BUILT.clear()
    world.app.dependency_overrides[get_sweep_factory] = _spy_sweep_factory

    preview = await world.http.get(SWEEP, params={"limit": 5}, headers=admin.headers)
    run = await world.http.post(SWEEP, json={"limit": None}, headers=admin.headers)

    assert preview.status_code == 200, preview.text
    assert run.status_code == 200, run.text
    assert [(limit, dry) for _, limit, dry in _SWEEP_BUILT] == [(5, True), (None, False)]
    for use_case, limit, dry_run in _SWEEP_BUILT:
        assert type(use_case) is ReclaimOrphanedFiles
        assert type(use_case._scanner) is LocalOrphanFileScanner
        assert type(use_case._data) is SqlAlchemyExpiredGuestData, (
            "the plain adapter: it never deletes rows"
        )
        assert type(use_case._files) is LocalFileStore
        assert use_case._grace == timedelta(hours=24)
        assert use_case._window == RetentionWindow(hours=world.settings.guest_retention_hours)
        assert use_case._limit == limit
        assert use_case._dry_run is dry_run


# ----------------------------------------------------------------------------- AC-15 storage


async def test_ac15_storage_splits_the_volume_into_five_buckets_by_count_and_bytes(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    guest = await world.guest()
    user = await world.account()
    g_entry = await world.entry(guest.owner)  # CV file + PDF export
    u_entry = await world.entry(user.owner, ready_formats=(ExportFormat.DOCX,))
    orphan = await world.orphan(body=b"o" * 37)
    part_ref = await world.orphan(body=b"x")
    part = world.path(part_ref)
    part.unlink()
    _write(part.with_name(part.name + ".part"), b"p" * 11)
    _write(world.upload_dir / "stray.bin", b"s" * 5)

    response = await world.http.get(STORAGE, headers=admin.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    body = response.json()
    assert set(body) == {"scanned_at", *BUCKETS}
    assert datetime.fromisoformat(body["scanned_at"]).tzinfo is not None
    assert body["guest"] == {
        "files": 2,
        "bytes": len(CV_BYTES) + len(export_bytes(g_entry.jobs[0].id)),
    }
    assert body["user"] == {
        "files": 2,
        "bytes": len(CV_BYTES) + len(export_bytes(u_entry.jobs[0].id)),
    }
    assert body["unreferenced"] == {"files": 1, "bytes": 37}
    assert body["partial"] == {"files": 1, "bytes": 11}
    assert body["unrecognized"] == {"files": 1, "bytes": 5}
    for bucket in BUCKETS:
        assert set(body[bucket]) == {"files", "bytes"}
    for leaked in (str(world.upload_dir), orphan.key, "stray.bin", ".part", ".pdf", ".docx"):
        assert leaked not in response.text, f"{leaked!r} reached the response"


async def test_ac15_an_empty_volume_is_five_zero_buckets(world: World) -> None:
    admin = await world.account(admin=True)

    response = await world.http.get(STORAGE, headers=admin.headers)

    assert response.status_code == 200, response.text
    assert {b: response.json()[b] for b in BUCKETS} == {
        b: {"files": 0, "bytes": 0} for b in BUCKETS
    }


async def test_ac15_a_volume_that_cannot_be_walked_is_503_storage_unavailable(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injected below the scanner's own `except OSError`, at the syscall."""
    admin = await world.account(admin=True)
    root = _real(world.upload_dir)
    original = os.scandir

    def _eio(path: Any = ".") -> Any:
        if os.path.realpath(os.fspath(path)) == root:
            raise OSError(5, "Input/output error")
        return original(path)

    monkeypatch.setattr(os, "scandir", _eio)

    response = await world.http.get(STORAGE, headers=admin.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "storage_unavailable"
    _no_store(response)


# ----------------------------------------------------------------------------- AC-16 preview


async def test_ac16_a_purge_preview_reports_what_a_batch_would_take_and_changes_nothing(
    world: World, redis: aioredis.Redis
) -> None:
    admin = await world.account(admin=True)
    b = await _backlog(world)
    rows_before = {
        "old": await world.rows_of_guest(b.old),
        "newer": await world.rows_of_guest(b.newer),
        "live": await world.rows_of_guest(b.live),
        "user": await world.rows_of_user(b.user),
    }

    response = await world.http.get(PURGE, params={"limit": 1}, headers=admin.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    body = response.json()
    assert set(body) == PURGE_KEYS
    assert body["dry_run"] is True
    assert body["lock"] is None
    assert (body["examined"], body["sessions_deleted"], body["sessions_skipped"]) == (1, 0, 0)
    assert (body["sessions_failed"], body["files_failed"]) == (0, 0)
    assert body["files_unlinked"] == 2, "what the oldest session would unlink"
    assert body["overdue"] == 2, "the backlog now, not the batch"
    await _expect_no_purge_state(redis)
    assert all(_exists(p) for p in b.all_files())
    assert rows_before == {
        "old": await world.rows_of_guest(b.old),
        "newer": await world.rows_of_guest(b.newer),
        "live": await world.rows_of_guest(b.live),
        "user": await world.rows_of_user(b.user),
    }


async def test_ac16_a_preview_without_a_limit_looks_at_one_batch_of_100(world: World) -> None:
    admin = await world.account(admin=True)
    await _backlog(world)

    response = await world.http.get(PURGE, headers=admin.headers)

    assert response.status_code == 200, response.text
    assert response.json()["examined"] == 2
    assert response.json()["files_unlinked"] == 3


# ----------------------------------------------------------------------------- AC-17 run


async def test_ac17_a_run_deletes_the_oldest_expired_sessions_up_to_limit_and_records_the_heartbeat(
    world: World, redis: aioredis.Redis
) -> None:
    admin = await world.account(admin=True)
    b = await _backlog(world)
    user_rows = await world.rows_of_user(b.user)
    live_rows = await world.rows_of_guest(b.live)

    response = await world.http.post(PURGE, json={"limit": 1}, headers=admin.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    body = response.json()
    assert set(body) == PURGE_KEYS
    assert body["dry_run"] is False
    assert body["lock"] == "acquired"
    assert (body["examined"], body["sessions_deleted"], body["sessions_skipped"]) == (1, 1, 0)
    assert (body["sessions_failed"], body["files_failed"], body["files_unlinked"]) == (0, 0, 2)
    assert body["overdue"] == 1, "the backlog after the run"
    assert set((await world.rows_of_guest(b.old)).values()) == {0}, "the oldest went"
    assert (await world.rows_of_guest(b.newer))["identity_guest_session"] == 1, "beyond limit"
    assert not any(_exists(p) for p in b.old_files)
    assert all(_exists(p) for p in [*b.newer_files, *b.live_files, *b.user_files])
    assert await world.rows_of_guest(b.live) == live_rows
    assert await world.rows_of_user(b.user) == user_rows
    beat = await _heartbeat(redis)
    assert beat is not None, "the heartbeat is written after a completed batch"
    assert (beat.outcome, beat.sessions_deleted, beat.files_unlinked) == ("ok", 1, 2)
    assert await redis.get(PURGE_LOCK_KEY) is None, "the lock is released"


async def test_ac17_the_default_limit_is_100_and_a_second_run_empties_the_backlog(
    world: World,
) -> None:
    admin = await world.account(admin=True)
    b = await _backlog(world)

    response = await world.http.post(PURGE, json={}, headers=admin.headers)

    assert response.status_code == 200, response.text
    assert response.json()["sessions_deleted"] == 2
    assert response.json()["overdue"] == 0
    assert not any(_exists(p) for p in [*b.old_files, *b.newer_files])
    again = await world.http.post(PURGE, json={}, headers=admin.headers)
    assert again.status_code == 200, "nothing to purge is still a completed run"
    assert (again.json()["examined"], again.json()["sessions_deleted"]) == (0, 0)


# ----------------------------------------------------------------------------- AC-18 held lock


async def test_ac18_a_held_lock_is_409_purge_in_progress_and_nothing_happens(
    world: World, redis: aioredis.Redis
) -> None:
    admin = await world.account(admin=True)
    b = await _backlog(world)
    assert await redis.set(PURGE_LOCK_KEY, "foreign-token", nx=True, px=60_000)
    old_rows = await world.rows_of_guest(b.old)

    response = await world.http.post(PURGE, json={"limit": 100}, headers=admin.headers)

    assert response.status_code == 409, response.text
    assert error_code(response) == "purge_in_progress"
    _no_store(response)
    assert await world.rows_of_guest(b.old) == old_rows, "a refused run deleted a row"
    assert all(_exists(p) for p in b.all_files()), "a refused run unlinked a file"
    assert await redis.exists(HEARTBEAT_KEY) == 0, "a refused run wrote a heartbeat"
    assert await redis.get(PURGE_LOCK_KEY) == b"foreign-token", (
        "the other holder's lock is untouched"
    )
    assert 0 < await redis.pttl(PURGE_LOCK_KEY) <= 60_000


async def test_ac18_a_held_lock_does_not_stop_a_preview(
    world: World, redis: aioredis.Redis
) -> None:
    admin = await world.account(admin=True)
    await _backlog(world)
    assert await redis.set(PURGE_LOCK_KEY, "foreign-token", nx=True, px=60_000)

    response = await world.http.get(PURGE, headers=admin.headers)

    assert response.status_code == 200, response.text
    assert response.json()["dry_run"] is True


# ----------------------------------------------------------------------------- AC-19 Redis down


async def test_ac19_an_unreachable_redis_still_runs_the_purge_and_says_the_lock_was_unavailable(
    world: World, redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injected below `RedisPurgeLock`'s own `except Exception`, at the client's command call —
    which is also what the heartbeat write reaches, so a failed heartbeat must not fail the run."""
    admin = await world.account(admin=True)
    b = await _backlog(world)

    async def _down(self: aioredis.Redis, *args: Any, **kwargs: Any) -> Any:
        raise aioredis.ConnectionError("simulated: redis unavailable (AC-19)")

    with monkeypatch.context() as patch:
        patch.setattr(aioredis.Redis, "execute_command", _down)
        response = await world.http.post(PURGE, json={"limit": 1}, headers=admin.headers)

    assert response.status_code == 200, response.text
    assert response.json()["lock"] == "unavailable"
    assert response.json()["sessions_deleted"] == 1, "the purge ran: FR-6 outranks the lock"
    assert set((await world.rows_of_guest(b.old)).values()) == {0}
    assert not any(_exists(p) for p in b.old_files)


# ----------------------------------------------------------------------------- AC-20 failure


async def test_ac20_a_run_that_raises_is_503_purge_failed_with_no_heartbeat_and_the_lock_released(
    world: World, redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injected at the **sync** `Session.execute` (both `execute` and `get` reach it), on the
    candidate read only: the bearer check and the backlog count still work."""
    admin = await world.account(admin=True)
    b = await _backlog(world)
    original = Session.execute

    def _down(self: Session, statement: Any, *args: Any, **kwargs: Any) -> Any:
        sql = str(statement)
        if (
            "identity_guest_session" in sql
            and "count(" not in sql.lower()
            and not sql.lstrip().upper().startswith("DELETE")
        ):
            raise SQLAlchemyError("simulated: postgres unavailable (AC-20)")
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", _down)

    response = await world.http.post(PURGE, json={"limit": 100}, headers=admin.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "purge_failed"
    _no_store(response)
    monkeypatch.undo()
    await _expect_no_purge_state(redis)
    assert (await world.rows_of_guest(b.old))["identity_guest_session"] == 1
    assert all(_exists(p) for p in b.all_files())


# ----------------------------------------------------------------------------- AC-21 sweep


async def _volume_with_one_of_each(world: World) -> dict[str, Path]:
    """An old orphan, an old *referenced* file, a young orphan, an old stray name."""
    guest = await world.guest()
    cv = await world.cv(guest.owner)
    world.age(cv.file.key, LONG_AGO)
    old_orphan = await world.orphan(age=LONG_AGO)
    young_orphan = await world.orphan(age=timedelta(minutes=1))
    _write(world.upload_dir / "stray.bin", b"stray")
    world.age("stray.bin", LONG_AGO)
    return {
        "old_orphan": world.path(old_orphan),
        "referenced": world.path(cv.file),
        "young_orphan": world.path(young_orphan),
        "stray": world.upload_dir / "stray.bin",
    }


async def test_ac21_a_sweep_preview_reports_the_counts_and_unlinks_nothing(
    world: World, redis: aioredis.Redis
) -> None:
    admin = await world.account(admin=True)
    files = await _volume_with_one_of_each(world)

    response = await world.http.get(SWEEP, headers=admin.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    body = response.json()
    assert set(body) == SWEEP_KEYS
    assert body["dry_run"] is True
    assert (body["scanned"], body["referenced"], body["unrecognized"]) == (3, 1, 1)
    assert (body["reclaimed"], body["failed"], body["too_young"]) == (1, 0, 0)
    assert all(_exists(p) for p in files.values()), "a preview unlinked a file"
    await _expect_no_purge_state(redis)


async def test_ac21_a_sweep_run_unlinks_the_unreferenced_past_the_floor_and_keeps_the_rest(
    world: World, redis: aioredis.Redis
) -> None:
    admin = await world.account(admin=True)
    files = await _volume_with_one_of_each(world)

    response = await world.http.post(SWEEP, json={"limit": None}, headers=admin.headers)

    assert response.status_code == 200, response.text
    _no_store(response)
    assert set(response.json()) == SWEEP_KEYS
    assert response.json()["dry_run"] is False
    assert (response.json()["reclaimed"], response.json()["failed"]) == (1, 0)
    assert not _exists(files["old_orphan"]), "the old orphan is reclaimed"
    assert _exists(files["referenced"]), "a referenced file is kept"
    assert _exists(files["young_orphan"]), "a young file is kept"
    assert _exists(files["stray"]), "an unrecognized name is counted, never deleted"
    await _expect_no_purge_state(redis)


async def test_ac21_when_the_database_cross_check_cannot_run_nothing_is_unlinked(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails closed. Injected at the sync `Session.execute`, on the statement that asks which keys
    a row names — the bearer check still works."""
    admin = await world.account(admin=True)
    files = await _volume_with_one_of_each(world)
    original = Session.execute

    def _down(self: Session, statement: Any, *args: Any, **kwargs: Any) -> Any:
        sql = str(statement)
        if "file_key" in sql and ("intake_base_cv" in sql or "export_job" in sql):
            raise SQLAlchemyError("simulated: postgres unavailable (AC-21)")
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "execute", _down)

    response = await world.http.post(SWEEP, json={}, headers=admin.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "orphan_check_unavailable"
    _no_store(response)
    assert all(_exists(p) for p in files.values()), "an unchecked file was unlinked"


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_ac21_a_volume_that_cannot_be_walked_is_503_storage_unavailable_on_both_sweep_routes(
    world: World, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    admin = await world.account(admin=True)
    root = _real(world.upload_dir)
    original = os.scandir

    def _eio(path: Any = ".") -> Any:
        if os.path.realpath(os.fspath(path)) == root:
            raise OSError(5, "Input/output error")
        return original(path)

    monkeypatch.setattr(os, "scandir", _eio)

    if method == "POST":
        response = await world.http.post(SWEEP, json={}, headers=admin.headers)
    else:
        response = await world.http.get(SWEEP, headers=admin.headers)

    assert response.status_code == 503, response.text
    assert error_code(response) == "storage_unavailable"


# ----------------------------------------------------------------------------- AC-22 validation

_BAD_QUERIES = [
    ("GET", PURGE, {"limit": "0"}),
    ("GET", PURGE, {"limit": "101"}),
    ("GET", PURGE, {"limit": "x"}),
    ("GET", PURGE, {"limit": "1.5"}),
    ("GET", SWEEP, {"limit": "0"}),
    ("GET", SWEEP, {"limit": "100001"}),
    ("GET", SWEEP, {"limit": "x"}),
]
_BAD_BODIES: list[tuple[str, dict[str, Any]]] = [
    (PURGE, {"limit": 0}),
    (PURGE, {"limit": 101}),
    (PURGE, {"limit": "x"}),
    (PURGE, {"limit": 1.5}),
    (PURGE, {"limit": 5, "dry_run": False}),
    (SWEEP, {"limit": 0}),
    (SWEEP, {"limit": 100_001}),
    (SWEEP, {"limit": "x"}),
    (SWEEP, {"limit": 5, "grace_hours": 0}),
]


@pytest.mark.parametrize(("method", "url", "params"), _BAD_QUERIES)
async def test_ac22_a_bad_limit_query_is_422_validation_error_and_nothing_runs(
    world: World, redis: aioredis.Redis, method: str, url: str, params: dict[str, str]
) -> None:
    admin = await world.account(admin=True)

    response = await world.http.request(method, url, params=params, headers=admin.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"
    await _expect_no_purge_state(redis)


@pytest.mark.parametrize(("url", "body"), _BAD_BODIES)
async def test_ac22_a_bad_body_is_422_validation_error_and_nothing_runs(
    world: World, redis: aioredis.Redis, url: str, body: dict[str, Any]
) -> None:
    admin = await world.account(admin=True)

    response = await world.http.post(url, json=body, headers=admin.headers)

    assert response.status_code == 422, response.text
    assert error_code(response) == "validation_error"
    await _expect_no_purge_state(redis)


# ----------------------------------------------------------------------------- AC-23 firewall

_ROUTES = [
    ("GET", STORAGE, None),
    ("GET", PURGE, None),
    ("POST", PURGE, {"limit": 100}),
    ("GET", SWEEP, None),
    ("POST", SWEEP, {"limit": None}),
]
_MALFORMED = [
    ("GET", f"{PURGE}?limit=0", None),
    ("GET", f"{PURGE}?limit=x", None),
    ("GET", f"{SWEEP}?limit=100001", None),
    ("POST", PURGE, {"limit": "x"}),
    ("POST", PURGE, {"limit": 5, "extra": 1}),
    ("POST", SWEEP, {"limit": 0}),
    ("POST", SWEEP, {"grace_hours": 0}),
]


async def test_ac23_a_plain_user_gets_the_404_of_an_unmatched_path_and_nothing_runs(
    world: World, redis: aioredis.Redis
) -> None:
    user = await world.account()
    b = await _backlog(world)
    old_rows = await world.rows_of_guest(b.old)
    unmatched = await world.http.get(UNMATCHED, headers=user.headers)
    assert unmatched.status_code == 404, unmatched.text

    for method, url, body in [*_ROUTES, *_MALFORMED]:
        kwargs: dict[str, Any] = {"json": body} if body is not None else {}
        refused = await world.http.request(method, url, headers=user.headers, **kwargs)
        assert refused.status_code == 404, f"{method} {url}: {refused.text}"
        assert refused.content == unmatched.content, f"{method} {url}"
        assert refused.json() == {"detail": "Not Found"}
        assert _comparable(refused) == _comparable(unmatched), f"{method} {url}"

    await _expect_no_purge_state(redis)
    assert await world.rows_of_guest(b.old) == old_rows, "a refused run changed a row"
    assert all(_exists(p) for p in b.all_files()), "a refused run removed a file"


async def test_ac23_anonymous_is_401_invalid_access_token_on_all_five(world: World) -> None:
    for method, url, body in _ROUTES:
        kwargs: dict[str, Any] = {"json": body} if body is not None else {}
        response = await world.http.request(method, url, **kwargs)
        assert response.status_code == 401, f"{method} {url}: {response.text}"
        assert error_code(response) == "invalid_access_token", f"{method} {url}"


async def test_ac23_a_disabled_admin_is_403_account_disabled_on_all_five(world: World) -> None:
    disabled, other = await world.account(admin=True), await world.account(admin=True)
    done = await world.http.post(f"/api/admin/users/{disabled.id}/disable", headers=other.headers)
    assert done.status_code == 200, done.text

    for method, url, body in _ROUTES:
        kwargs: dict[str, Any] = {"json": body} if body is not None else {}
        response = await world.http.request(method, url, headers=disabled.headers, **kwargs)
        assert response.status_code == 403, f"{method} {url}: {response.text}"
        assert error_code(response) == "account_disabled", f"{method} {url}"
