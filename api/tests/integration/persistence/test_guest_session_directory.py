"""Persistence tests for slice 4.4's guest-data reads (T12, test-after), on real Postgres.

Covers `SqlGuestSessionDirectory` (live only, `expires_at DESC, id DESC`, keyset ties across pages,
per-table counts, `get` live-only), AC-28 (no `token_hash` in the compiled statements), and the two
new `SqlAlchemyExpiredGuestData` reads: `session_with_files` and `owners_of`.

`tailorcraft_test` may hold sessions other tests committed, so every assertion is scoped to the ids
this test created. Rows go through the domain's grammars (`FileRef`, the real aggregates) via the
retention suite's `_Rig`; files land in a per-test `tmp_path`, never the shared `upload_dir`.

**Mutation 10 (AC-28), recorded:** `_s.token_hash` added to `_summaries`' select in
`guest_session_directory.py` -> `test_neither_page_nor_get_names_token_hash` red
(`AssertionError: token_hash in page statement`), 1 failed; source restored byte-exact.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat, LayoutTemplate
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import CvContentType, OriginalFilename
from tailorcraft.domain.retention.guest_sessions import GuestSessionCursor
from tailorcraft.domain.retention.storage import FileOwnerKind
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.persistence.queries.guest_session_directory import (
    SqlGuestSessionDirectory,
)
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
    SqlAlchemyExpiredGuestData,
)
from tests.integration.persistence.owner_rows import persist_user
from tests.integration.retention.test_purge_database import _Rig

# A band no other test seeds into, so a keyset walk from its top meets only this test's rows first.
_BAND = datetime(2100, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def rig(session: AsyncSession, clock: FixedClock, tmp_path: Path) -> _Rig:
    return _Rig(session=session, clock=clock, files=LocalFileStore(tmp_path))


@contextmanager
def _captured_selects(session: AsyncSession) -> Iterator[list[str]]:
    statements: list[str] = []
    engine = session.get_bind()

    def capture(conn: Connection, cursor: object, statement: str, *args: object) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def _top_cursor() -> GuestSessionCursor:
    return GuestSessionCursor(
        expires_at=_BAND + timedelta(days=1), session_id=GuestSessionId(UUID(int=(1 << 128) - 1))
    )


async def _walk(
    directory: SqlGuestSessionDirectory, as_of: datetime, mine: set[GuestSessionId], limit: int
) -> list[GuestSessionId]:
    """Every page from the band's top, concatenated; asserts no id repeats across pages."""
    seen: list[GuestSessionId] = []
    cursor: GuestSessionCursor | None = _top_cursor()
    while cursor is not None:
        page = await directory.page(as_of, cursor, limit)
        seen.extend(s.session_id for s in page.items)
        cursor = page.next_cursor
        if len(set(seen)) == len(mine & set(seen)) and mine <= set(seen):
            break  # everything of ours seen; leftovers below the band are not ours to judge
    assert len(seen) == len(set(seen)), "an id repeated across pages"
    return [i for i in seen if i in mine]


# --- directory ------------------------------------------------------------------------------------


async def test_only_live_sessions_are_listed_and_the_cutoff_instant_is_expired(
    session: AsyncSession, rig: _Rig, clock: FixedClock
) -> None:
    now = clock.now()
    expired = await rig.new_guest_session(expires_at=now - timedelta(seconds=1))
    exactly_now = await rig.new_guest_session(expires_at=now)
    live = await rig.new_guest_session(expires_at=now + timedelta(seconds=1))
    directory = SqlGuestSessionDirectory(session)

    page = await directory.page(now, None, 50)

    ids = {s.session_id for s in page.items}
    assert live in ids
    assert expired not in ids
    assert exactly_now not in ids


async def test_sessions_come_back_by_expiry_descending_then_id_descending(
    session: AsyncSession, rig: _Rig, clock: FixedClock
) -> None:
    early = await rig.new_guest_session(expires_at=_BAND)
    late = await rig.new_guest_session(expires_at=_BAND + timedelta(hours=1))
    tied = [await rig.new_guest_session(expires_at=_BAND + timedelta(minutes=30)) for _ in range(3)]
    directory = SqlGuestSessionDirectory(session)

    walked = await _walk(directory, clock.now(), {early, late, *tied}, 50)

    tied_desc = sorted(tied, key=lambda s: s.value, reverse=True)
    assert walked == [late, *tied_desc, early]


async def test_sessions_with_equal_expiry_never_repeat_or_skip_across_pages(
    session: AsyncSession, rig: _Rig, clock: FixedClock
) -> None:
    tied = [await rig.new_guest_session(expires_at=_BAND) for _ in range(5)]
    directory = SqlGuestSessionDirectory(session)

    walked = await _walk(directory, clock.now(), set(tied), 2)

    assert walked == sorted(tied, key=lambda s: s.value, reverse=True)


async def test_next_cursor_names_the_last_item_and_is_absent_exactly_when_nothing_remains(
    session: AsyncSession, rig: _Rig, clock: FixedClock
) -> None:
    first = await rig.new_guest_session(expires_at=_BAND + timedelta(hours=1))
    second = await rig.new_guest_session(expires_at=_BAND)
    directory = SqlGuestSessionDirectory(session)
    now = clock.now()

    one = await directory.page(now, _top_cursor(), 1)
    assert [s.session_id for s in one.items] == [first]
    assert one.next_cursor == GuestSessionCursor(
        expires_at=_BAND + timedelta(hours=1), session_id=first
    )
    after = await directory.page(now, one.next_cursor, 1)
    assert [s.session_id for s in after.items] == [second]

    total = len((await directory.page(now, None, 500)).items)
    assert (await directory.page(now, None, total)).next_cursor is None
    assert (await directory.page(now, None, total - 1)).next_cursor is not None


async def test_counts_are_per_table_and_never_include_another_sessions_or_a_users_rows(
    session: AsyncSession, rig: _Rig, clock: FixedClock, tmp_path: Path
) -> None:
    a = await rig.new_guest_session(expires_at=_BAND)
    b = await rig.new_guest_session(expires_at=_BAND + timedelta(hours=1))
    for _ in range(2):
        await rig.add_base_cv(a)
    await rig.add_job_posting(a)
    for _ in range(3):
        await rig.add_tailoring_run(a)
    await rig.add_ready_export_job(a)
    await rig.add_rendering_export_job_with_bytes_on_disk(a)
    await rig.add_base_cv(b)
    await _user_cv_and_export(session, clock)
    directory = SqlGuestSessionDirectory(session)

    got_a = await directory.get(a, clock.now())
    got_b = await directory.get(b, clock.now())

    assert got_a is not None
    assert got_b is not None
    assert (got_a.base_cvs, got_a.job_postings, got_a.tailoring_runs, got_a.export_jobs) == (
        2,
        1,
        3,
        2,
    )
    assert (got_b.base_cvs, got_b.job_postings, got_b.tailoring_runs, got_b.export_jobs) == (
        1,
        0,
        0,
        0,
    )
    page = await directory.page(clock.now(), _top_cursor(), 2)
    assert next(s for s in page.items if s.session_id == a) == got_a


async def test_get_answers_none_for_an_expired_unknown_or_boundary_session(
    session: AsyncSession, rig: _Rig, clock: FixedClock
) -> None:
    now = clock.now()
    expired = await rig.new_guest_session(expires_at=now - timedelta(minutes=1))
    boundary = await rig.new_guest_session(expires_at=now)
    live = await rig.new_guest_session(expires_at=now + timedelta(minutes=1))
    directory = SqlGuestSessionDirectory(session)

    assert await directory.get(expired, now) is None
    assert await directory.get(boundary, now) is None
    assert await directory.get(GuestSessionId(uuid4()), now) is None
    got = await directory.get(live, now)
    assert got is not None
    assert got.session_id == live


# --- AC-28 ----------------------------------------------------------------------------------------


async def test_neither_page_nor_get_names_token_hash(
    session: AsyncSession, rig: _Rig, clock: FixedClock
) -> None:
    a = await rig.new_guest_session(expires_at=_BAND)
    directory = SqlGuestSessionDirectory(session)

    with _captured_selects(session) as page_sql:
        await directory.page(clock.now(), _top_cursor(), 5)
    with _captured_selects(session) as get_sql:
        await directory.get(a, clock.now())

    assert page_sql
    assert get_sql
    assert "token_hash" not in " ".join(page_sql).lower(), "token_hash in page statement"
    assert "token_hash" not in " ".join(get_sql).lower(), "token_hash in get statement"


# --- session_with_files ---------------------------------------------------------------------------


async def test_session_with_files_returns_live_and_expired_sessions_with_derived_export_keys(
    session: AsyncSession, rig: _Rig, clock: FixedClock
) -> None:
    now = clock.now()
    live = await rig.new_guest_session(expires_at=now + timedelta(hours=1))
    expired = await rig.new_guest_session(expires_at=now - timedelta(hours=1))
    live_files = {
        await rig.add_base_cv(live),
        await rig.add_ready_export_job(live),
        await rig.add_rendering_export_job_with_bytes_on_disk(live),  # file_key NULL: derived
    }
    expired_cv = await rig.add_base_cv(expired)
    data = SqlAlchemyExpiredGuestData(session)

    got_live = await data.session_with_files(live)
    got_expired = await data.session_with_files(expired)

    assert got_live is not None
    assert got_expired is not None
    assert got_live.session_id == live
    assert got_live.expires_at == now + timedelta(hours=1)
    assert set(got_live.files) == live_files
    assert got_expired.files == (expired_cv,)


async def test_session_with_files_of_an_unknown_session_is_none(session: AsyncSession) -> None:
    assert (
        await SqlAlchemyExpiredGuestData(session).session_with_files(GuestSessionId(uuid4()))
        is None
    )


async def test_session_with_files_selects_no_token_hash(
    session: AsyncSession, rig: _Rig, clock: FixedClock
) -> None:
    s = await rig.new_guest_session(expires_at=_BAND)
    with _captured_selects(session) as sql:
        await SqlAlchemyExpiredGuestData(session).session_with_files(s)
    assert sql
    assert "token_hash" not in " ".join(sql).lower()


# --- owners_of ------------------------------------------------------------------------------------


async def _user_cv_and_export(session: AsyncSession, clock: FixedClock) -> tuple[FileRef, FileRef]:
    owner = await persist_user(session, clock)
    return await _user_rows(session, clock, owner)


async def _user_rows(
    session: AsyncSession, clock: FixedClock, owner: UserOwner
) -> tuple[FileRef, FileRef]:
    cvs = SqlAlchemyBaseCvRepository(session)
    cv_id = cvs.next_identity()
    cv_ref = FileRef.for_base_cv(cv_id, CvContentType.PDF)
    await cvs.add(
        BaseCv.upload(
            id=cv_id,
            owner=owner,
            original_filename=OriginalFilename("cv.pdf"),
            content_type=CvContentType.PDF,
            size_bytes=8,
            file=cv_ref,
            uploaded_at=clock.now(),
        )
    )
    jobs = SqlAlchemyExportJobRepository(session)
    job = ExportJob.request(
        id=jobs.next_identity(),
        owner=owner,
        tailoring_run_id=TailoringRunId(value=uuid4()),
        document=TailoredDocumentKind.CV,
        format=ExportFormat.PDF,
        layout_template=LayoutTemplate.CLASSIC,
        run_version=1,
        requested_at=clock.now(),
    )
    job.mark_started(clock.now())
    job.mark_ready(byte_size=9, render_duration_ms=50, at=clock.now())
    await jobs.add(job)
    await session.flush()
    return cv_ref, job.storage_ref


def _absent() -> FileRef:
    u = uuid4()
    return FileRef(key=f"{u.hex[0:2]}/{u.hex[2:4]}/{u}.pdf")


async def test_owners_of_names_guest_user_and_leaves_absent_keys_out(
    session: AsyncSession, rig: _Rig, clock: FixedClock
) -> None:
    g = await rig.new_guest_session(expires_at=_BAND)
    guest_cv = await rig.add_base_cv(g)
    guest_export = await rig.add_ready_export_job(g)
    user_cv, user_export = await _user_cv_and_export(session, clock)
    nobody = _absent()

    got = await SqlAlchemyExpiredGuestData(session).owners_of(
        [guest_cv, guest_export, user_cv, user_export, nobody]
    )

    assert got == {
        guest_cv: FileOwnerKind.GUEST,
        guest_export: FileOwnerKind.GUEST,
        user_cv: FileOwnerKind.USER,
        user_export: FileOwnerKind.USER,
    }


async def test_owners_of_matches_stored_keys_so_a_rendering_job_reads_as_unreferenced(
    session: AsyncSession, rig: _Rig
) -> None:
    """Decision (T10, spec glossary): the stored `file_key` is matched, not the derived key; a
    `rendering` job's bytes are 'mid-write', exactly as the orphan sweep sees them."""
    g = await rig.new_guest_session(expires_at=_BAND)
    rendering = await rig.add_rendering_export_job_with_bytes_on_disk(g)

    assert await SqlAlchemyExpiredGuestData(session).owners_of([rendering]) == {}


async def test_owners_of_answers_across_chunk_boundaries_for_2500_keys(
    session: AsyncSession, rig: _Rig, clock: FixedClock
) -> None:
    g = await rig.new_guest_session(expires_at=_BAND)
    guest_cv = await rig.add_base_cv(g)
    user_cv, _ = await _user_cv_and_export(session, clock)
    guest_export = await rig.add_ready_export_job(g)
    keys = [_absent() for _ in range(2500)]
    keys[5], keys[1500], keys[2499] = guest_cv, user_cv, guest_export
    keys.append(guest_cv)  # a duplicate must not break the answer

    got = await SqlAlchemyExpiredGuestData(session).owners_of(keys)

    assert got == {
        guest_cv: FileOwnerKind.GUEST,
        user_cv: FileOwnerKind.USER,
        guest_export: FileOwnerKind.GUEST,
    }
