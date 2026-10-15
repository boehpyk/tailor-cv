"""Application tests for slice 4.4's four read/erase use cases (T8 RED, AC-4 ... AC-7).

Pure hand-written fakes, no database (precedent: `test_reclaim_orphaned_files.py`). Every absence
assertion ("nothing unlinked") is paired with a positive one made first, because the skeleton's
`NotImplementedError` (a `RuntimeError`) would satisfy a bare absence check. Exceptions that must
"escape" use a private `Exception` subclass, never `RuntimeError`, for the same reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.retention.erase_guest_session import EraseGuestSession
from tailorcraft.application.retention.get_guest_session_overview import GetGuestSessionOverview
from tailorcraft.application.retention.list_live_guest_sessions import ListLiveGuestSessions
from tailorcraft.application.retention.summarize_storage import SummarizeStorage
from tailorcraft.domain.identity.errors import GuestSessionNotFound
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.retention.guest_sessions import (
    PAGE_SIZE_MAX,
    GuestSessionCursor,
    GuestSessionPage,
    GuestSessionSummary,
)
from tailorcraft.domain.retention.storage import FileOwnerKind, StorageBucket
from tailorcraft.domain.retention.value_objects import (
    ExpiringGuestSession,
    FileUnlinkFailure,
    ScannedFile,
)
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable
from tailorcraft.infrastructure.clock import FixedClock

NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)


def _ref() -> FileRef:
    g = uuid4()
    return FileRef(key=f"{g.hex[0:2]}/{g.hex[2:4]}/{g}.pdf")


def _summary(sid: GuestSessionId | None = None) -> GuestSessionSummary:
    return GuestSessionSummary(
        session_id=sid or GuestSessionId(uuid4()),
        created_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=23),
        base_cvs=1,
        job_postings=2,
        tailoring_runs=3,
        export_jobs=4,
    )


class _Boom(Exception):
    pass


# --- AC-4 --------------------------------------------------------------------------------------


@dataclass
class _Directory:
    page_result: GuestSessionPage
    got: GuestSessionSummary | None = None
    page_calls: list[tuple[datetime, GuestSessionCursor | None, int]] = field(default_factory=list)
    get_calls: list[tuple[GuestSessionId, datetime]] = field(default_factory=list)

    async def page(
        self, as_of: datetime, after: GuestSessionCursor | None, limit: int
    ) -> GuestSessionPage:
        self.page_calls.append((as_of, after, limit))
        return self.page_result

    async def get(self, session_id: GuestSessionId, as_of: datetime) -> GuestSessionSummary | None:
        self.get_calls.append((session_id, as_of))
        return self.got


async def test_listing_passes_the_clocks_now_and_returns_the_page_unchanged() -> None:
    cursor = GuestSessionCursor(expires_at=NOW, session_id=GuestSessionId(uuid4()))
    page = GuestSessionPage(items=(_summary(),), next_cursor=cursor)
    directory = _Directory(page_result=page)

    result = await ListLiveGuestSessions(directory, FixedClock(NOW))(after=cursor, limit=20)

    assert result is page
    assert directory.page_calls == [(NOW, cursor, 20)]


@pytest.mark.parametrize("limit", [0, -1, PAGE_SIZE_MAX + 1])
async def test_listing_refuses_a_limit_outside_one_to_the_maximum(limit: int) -> None:
    directory = _Directory(page_result=GuestSessionPage(items=(), next_cursor=None))

    with pytest.raises(InvariantViolated):
        await ListLiveGuestSessions(directory, FixedClock(NOW))(after=None, limit=limit)

    assert directory.page_calls == []


@pytest.mark.parametrize("limit", [1, PAGE_SIZE_MAX])
async def test_listing_accepts_the_bounds_themselves(limit: int) -> None:
    directory = _Directory(page_result=GuestSessionPage(items=(), next_cursor=None))

    await ListLiveGuestSessions(directory, FixedClock(NOW))(after=None, limit=limit)

    assert directory.page_calls == [(NOW, None, limit)]


# --- AC-5 --------------------------------------------------------------------------------------


async def test_overview_returns_the_summary_of_a_live_session() -> None:
    summary = _summary()
    directory = _Directory(page_result=GuestSessionPage((), None), got=summary)

    result = await GetGuestSessionOverview(directory, FixedClock(NOW))(summary.session_id)

    assert result == summary
    assert directory.get_calls == [(summary.session_id, NOW)]


async def test_overview_of_an_absent_or_expired_session_is_guest_session_not_found() -> None:
    # The query port answers `None` for both "never existed" and "expired" (as_of is the clock's).
    sid = GuestSessionId(uuid4())
    directory = _Directory(page_result=GuestSessionPage((), None), got=None)

    with pytest.raises(GuestSessionNotFound):
        await GetGuestSessionOverview(directory, FixedClock(NOW))(sid)

    assert directory.get_calls == [(sid, NOW)]


# --- AC-6 --------------------------------------------------------------------------------------


@dataclass
class _Data:
    candidate: ExpiringGuestSession | None
    deleted: bool = True
    delete_raises: Exception | None = None
    events: list[str] = field(default_factory=list)

    async def session_with_files(self, session_id: GuestSessionId) -> ExpiringGuestSession | None:
        self.events.append("lookup")
        return self.candidate

    async def delete_session(self, session_id: GuestSessionId) -> bool:
        self.events.append("delete_session")
        if self.delete_raises is not None:
            raise self.delete_raises
        return self.deleted


@dataclass
class _Files:
    events: list[str]
    fail_on: FileRef | None = None
    unlinked: list[FileRef] = field(default_factory=list)

    async def delete(self, ref: FileRef) -> None:
        self.events.append("files.delete")
        if ref == self.fail_on:
            raise FileStoreUnavailable("boom")
        self.unlinked.append(ref)


def _candidate(
    n: int = 2, *, expires_at: datetime = NOW + timedelta(hours=5)
) -> ExpiringGuestSession:
    return ExpiringGuestSession(
        session_id=GuestSessionId(uuid4()),
        expires_at=expires_at,
        files=tuple(_ref() for _ in range(n)),
    )


def _erase(data: _Data, files: _Files) -> EraseGuestSession:
    return EraseGuestSession(data, files)  # type: ignore[arg-type]


async def test_erasing_an_unknown_session_is_not_found_and_touches_nothing() -> None:
    data = _Data(candidate=None)
    files = _Files(data.events)

    with pytest.raises(GuestSessionNotFound):
        await _erase(data, files)(GuestSessionId(uuid4()))

    assert data.events == ["lookup"]  # looked up, nothing deleted, nothing unlinked


async def test_erase_commits_the_row_delete_before_any_file_unlink() -> None:
    cand = _candidate(3)
    data = _Data(candidate=cand)
    files = _Files(data.events)

    report = await _erase(data, files)(cand.session_id)

    assert report.files_unlinked == 3
    assert report.file_unlink_failures == ()
    assert files.unlinked == list(cand.files)
    assert data.events == [
        "lookup",
        "delete_session",
        "files.delete",
        "files.delete",
        "files.delete",
    ]


async def test_erase_losing_the_delete_to_a_claim_is_not_found_and_unlinks_nothing() -> None:
    cand = _candidate(2)
    data = _Data(candidate=cand, deleted=False)
    files = _Files(data.events)

    with pytest.raises(GuestSessionNotFound):
        await _erase(data, files)(cand.session_id)

    assert data.events == ["lookup", "delete_session"]
    assert files.unlinked == []


async def test_erase_records_a_refused_unlink_by_type_name_and_unlinks_the_rest() -> None:
    cand = _candidate(3)
    data = _Data(candidate=cand)
    files = _Files(data.events, fail_on=cand.files[1])

    report = await _erase(data, files)(cand.session_id)

    assert report.files_unlinked == 2
    assert report.file_unlink_failures == (FileUnlinkFailure(error_type="FileStoreUnavailable"),)
    assert files.unlinked == [cand.files[0], cand.files[2]]
    assert cand.files[1].key not in repr(report)


async def test_erase_lets_a_refused_row_delete_escape_and_unlinks_nothing() -> None:
    cand = _candidate(2)
    data = _Data(candidate=cand, delete_raises=_Boom())
    files = _Files(data.events)

    with pytest.raises(_Boom):
        await _erase(data, files)(cand.session_id)

    assert data.events == ["lookup", "delete_session"]
    assert files.unlinked == []


@pytest.mark.parametrize(
    "delta", [timedelta(hours=5), -timedelta(hours=5)], ids=["live", "expired"]
)
async def test_erase_works_on_a_live_and_on_an_expired_session(delta: timedelta) -> None:
    cand = _candidate(1, expires_at=NOW + delta)
    data = _Data(candidate=cand)
    files = _Files(data.events)

    report = await _erase(data, files)(cand.session_id)

    assert report.files_unlinked == 1
    assert data.events == ["lookup", "delete_session", "files.delete"]


# --- AC-7 --------------------------------------------------------------------------------------


@dataclass
class _Scanner:
    entries: list[ScannedFile]
    cutoffs: list[datetime] = field(default_factory=list)

    async def scan_older_than(self, cutoff: datetime) -> Sequence[ScannedFile]:
        self.cutoffs.append(cutoff)
        return self.entries


@dataclass
class _Owners:
    owners: dict[FileRef, FileOwnerKind]
    raises: Exception | None = None
    calls: list[tuple[FileRef, ...]] = field(default_factory=list)

    async def owners_of(self, keys: Sequence[FileRef]) -> Mapping[FileRef, FileOwnerKind]:
        self.calls.append(tuple(keys))
        if self.raises is not None:
            raise self.raises
        return {k: v for k, v in self.owners.items() if k in keys}


def _file(ref: FileRef | None, size: int, *, partial: bool = False) -> ScannedFile:
    return ScannedFile(ref=ref, created_at=NOW, is_partial=partial, size_bytes=size)


async def test_storage_overview_buckets_every_entry_and_sums_bytes() -> None:
    g1, g2, u1, orphan, part = _ref(), _ref(), _ref(), _ref(), _ref()
    scanner = _Scanner(
        [
            _file(g1, 10),
            _file(g2, 20),
            _file(u1, 100),
            _file(orphan, 1000),
            _file(part, 7, partial=True),
            _file(None, 5),
            _file(None, 6),
        ]
    )
    data = _Owners({g1: FileOwnerKind.GUEST, g2: FileOwnerKind.GUEST, u1: FileOwnerKind.USER})

    overview = await SummarizeStorage(scanner, data, FixedClock(NOW))()  # type: ignore[arg-type]

    assert overview.guest == StorageBucket(files=2, bytes=30)
    assert overview.user == StorageBucket(files=1, bytes=100)
    assert overview.unreferenced == StorageBucket(files=1, bytes=1000)
    assert overview.partial == StorageBucket(files=1, bytes=7)
    assert overview.unrecognized == StorageBucket(files=2, bytes=11)
    assert overview.scanned_at == NOW


async def test_storage_walks_once_with_the_cutoff_a_day_ahead_of_the_clocks_now() -> None:
    scanner = _Scanner([_file(_ref(), 1)])

    await SummarizeStorage(scanner, _Owners({}), FixedClock(NOW))()  # type: ignore[arg-type]

    assert scanner.cutoffs == [NOW + timedelta(days=1)]


async def test_storage_asks_owners_once_over_only_recognised_non_partial_keys() -> None:
    a, b, part = _ref(), _ref(), _ref()
    scanner = _Scanner([_file(a, 1), _file(part, 2, partial=True), _file(None, 3), _file(b, 4)])
    data = _Owners({})

    overview = await SummarizeStorage(scanner, data, FixedClock(NOW))()  # type: ignore[arg-type]

    assert overview.unreferenced == StorageBucket(files=2, bytes=5)  # positive first
    assert len(data.calls) == 1
    assert sorted(data.calls[0], key=lambda r: r.key) == sorted([a, b], key=lambda r: r.key)


async def test_storage_owners_failure_escapes_with_no_half_attributed_overview() -> None:
    scanner = _Scanner([_file(_ref(), 1)])
    data = _Owners({}, raises=_Boom())

    with pytest.raises(_Boom):
        await SummarizeStorage(scanner, data, FixedClock(NOW))()  # type: ignore[arg-type]

    assert len(data.calls) == 1  # it did ask; the failure is what escaped
