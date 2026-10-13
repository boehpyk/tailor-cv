"""AC-6 and AC-7 end to end (slice 4.4, T12): `EraseGuestSession` and `SummarizeStorage` over a real
Postgres (real commits, read back on a **separate** `engine.connect()`) and a real `tmp_path`
filesystem — never the shared `upload_dir`. Assertions read the tables and the disk, not the report
the code under test produced (R-42), except where the report is the contract (the storage buckets).

Also the scanner's `size_bytes` against the real file size (the number AC-7's bytes are summed from).
"""

# ruff: noqa: F811  (pytest fixtures imported by name, then requested by parameter)

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from tailorcraft.application.retention.erase_guest_session import EraseGuestSession
from tailorcraft.application.retention.summarize_storage import SummarizeStorage
from tailorcraft.domain.identity.errors import GuestSessionNotFound
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType
from tailorcraft.domain.retention.storage import StorageBucket
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.files.orphan_scanner import LocalOrphanFileScanner
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
    SqlAlchemyExpiredGuestData,
)
from tailorcraft.infrastructure.retention.data_access import CommittingExpiredGuestDataAdapter
from tailorcraft.infrastructure.settings import Settings
from tests.integration.persistence.owner_rows import persist_user
from tests.integration.persistence.test_guest_session_directory import _user_rows
from tests.integration.retention.test_purge_database import (  # noqa: F401
    _assert_test_database,
    _Rig,
    committing_connection,
    committing_session,
)


@pytest_asyncio.fixture
async def rig(
    committing_session: AsyncSession, clock: FixedClock, tmp_path: Path
) -> AsyncIterator[_Rig]:
    r = _Rig(session=committing_session, clock=clock, files=LocalFileStore(tmp_path))
    try:
        yield r
    finally:
        await r.cleanup()


def _age_all_to(root: Path, clock: FixedClock) -> None:
    """Files carry the real wall-clock mtime; the fixed test clock is in the past, so pin every
    mtime to it (the scan's cutoff is `clock.now() + 1 day`)."""
    stamp = clock.now().timestamp()
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (stamp, stamp))


def _erase(rig: _Rig) -> EraseGuestSession:
    data = CommittingExpiredGuestDataAdapter(SqlAlchemyExpiredGuestData(rig.session), rig.session)
    return EraseGuestSession(data=data, files=rig.files)


async def _owned(conn: AsyncConnection, owner: GuestSessionId) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, table in (
        ("cv", base_cv_table),
        ("posting", job_posting_table),
        ("run", tailoring_run_table),
        ("export", export_job_table),
        ("session", guest_session_table),
    ):
        col = table.c.id if name == "session" else table.c.guest_session_id
        key = owner
        counts[name] = (
            await conn.execute(select(func.count()).select_from(table).where(col == key))
        ).scalar_one()
    return counts


# --- scanner size ---------------------------------------------------------------------------------


async def test_the_scanner_reports_the_real_file_size_in_bytes(
    tmp_path: Path, clock: FixedClock
) -> None:
    store = LocalFileStore(tmp_path)
    ref = FileRef.for_base_cv(BaseCvId(value=uuid4()), CvContentType.PDF)
    await store.put(ref, b"x" * 1234)
    _age_all_to(tmp_path, clock)

    scanned = await LocalOrphanFileScanner(tmp_path).scan_older_than(
        clock.now() + timedelta(days=1)
    )

    assert [(f.ref, f.size_bytes) for f in scanned] == [(ref, os.stat(tmp_path / ref.key).st_size)]
    assert scanned[0].size_bytes == 1234


# --- AC-6 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("hours", [1, -1], ids=["live", "expired"])
async def test_erasing_a_session_removes_its_rows_and_files_and_spares_another(
    settings: Settings,
    engine: AsyncEngine,
    rig: _Rig,
    clock: FixedClock,
    tmp_path: Path,
    hours: int,
) -> None:
    _assert_test_database(settings)
    target = await rig.new_guest_session(expires_at=clock.now() + timedelta(hours=hours))
    other = await rig.new_guest_session(expires_at=clock.now() + timedelta(hours=1))
    target_files = [
        await rig.add_base_cv(target),
        await rig.add_ready_export_job(target),
        await rig.add_rendering_export_job_with_bytes_on_disk(target),
    ]
    await rig.add_job_posting(target)
    await rig.add_tailoring_run(target)
    other_files = [await rig.add_base_cv(other), await rig.add_ready_export_job(other)]
    await rig.add_job_posting(other)
    for ref in target_files + other_files:
        assert (tmp_path / ref.key).exists()

    await _erase(rig)(target)

    async with engine.connect() as conn:  # a separate connection: only committed state is visible
        assert set((await _owned(conn, target)).values()) == {0}
        assert await _owned(conn, other) == {
            "cv": 1,
            "posting": 1,
            "run": 0,
            "export": 1,
            "session": 1,
        }
    for ref in target_files:
        assert not (tmp_path / ref.key).exists(), f"{ref.key} survived"
    for ref in other_files:
        assert (tmp_path / ref.key).exists()


async def test_erasing_an_unknown_session_is_not_found_and_touches_nothing(
    settings: Settings, rig: _Rig, clock: FixedClock, tmp_path: Path
) -> None:
    _assert_test_database(settings)
    other = await rig.new_guest_session(expires_at=clock.now() + timedelta(hours=1))
    ref = await rig.add_base_cv(other)

    with pytest.raises(GuestSessionNotFound):
        await _erase(rig)(GuestSessionId(uuid4()))

    assert (tmp_path / ref.key).exists()


# --- AC-7 -----------------------------------------------------------------------------------------


async def test_the_storage_buckets_match_the_files_seeded_on_a_real_volume(
    settings: Settings, rig: _Rig, clock: FixedClock, tmp_path: Path
) -> None:
    _assert_test_database(settings)
    guest = await rig.new_guest_session(expires_at=clock.now() + timedelta(hours=1))
    guest_cv = await rig.add_base_cv(guest)  # 8 bytes
    guest_export = await rig.add_ready_export_job(guest)  # 8 bytes
    mid_write = await rig.add_rendering_export_job_with_bytes_on_disk(
        guest
    )  # NULL key: unreferenced
    user_owner = await persist_user(rig.session, clock)
    user_ref, user_export = await _user_rows(rig.session, clock, user_owner)
    await rig.session.commit()
    await rig.files.put(user_ref, b"u" * 13)
    await rig.files.put(user_export, b"e" * 17)
    stray = FileRef.for_base_cv(BaseCvId(value=uuid4()), CvContentType.PDF)
    await rig.files.put(stray, b"s" * 5)
    (tmp_path / f"{stray.key}.part").write_bytes(b"p" * 7)
    (tmp_path / "notes.txt").write_bytes(b"u" * 11)
    _age_all_to(tmp_path, clock)

    try:
        data = CommittingExpiredGuestDataAdapter(
            SqlAlchemyExpiredGuestData(rig.session), rig.session
        )
        overview = await SummarizeStorage(LocalOrphanFileScanner(tmp_path), data, clock)()

        sizes = {
            r: (tmp_path / r.key).stat().st_size
            for r in (guest_cv, guest_export, mid_write, user_ref, user_export, stray)
        }
        assert overview.guest == StorageBucket(files=2, bytes=sizes[guest_cv] + sizes[guest_export])
        assert overview.user == StorageBucket(files=2, bytes=sizes[user_ref] + sizes[user_export])
        assert overview.unreferenced == StorageBucket(
            files=2, bytes=sizes[mid_write] + sizes[stray]
        )
        assert overview.partial == StorageBucket(files=1, bytes=7)
        assert overview.unrecognized == StorageBucket(files=1, bytes=11)
        assert overview.scanned_at == clock.now()
    finally:
        await rig.session.execute(user_table.delete().where(user_table.c.id == user_owner.user_id))
        await rig.session.commit()
