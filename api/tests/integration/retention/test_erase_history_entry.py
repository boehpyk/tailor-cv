"""Application tests for `EraseHistoryEntry` (slice 2.3, T10 RED, AC-14, H-31, H-42, H-44).

`EraseHistoryEntry(user_id, run_id)`: authorizes through `GetTailoringRun(run_id, UserOwner(user_id))`
(the 404 collapse, inherited); a `queued` or `running` run raises `HistoryEntryInProgress` with
**nothing deleted**; otherwise `delete_history_entry` (rows, committed by the bound adapter)
**then** each returned export key is unlinked; unlink failures come back as exception **type names**,
never raised; a second call raises `TailoringRunNotFound` and unlinks nothing. **The use case does
not log.**

**Order is asserted with recording doubles** that append to one shared list: the run's load
(`get`), the rows' deletion (`delete_history_entry`), each unlink (`unlink`). `RecordingHistoryEntryData`
really removes the rows from the in-memory repositories, so "the second call finds nothing" is a
consequence rather than a flag.

**Every absence is paired with a positive.** The skeleton deletes nothing and unlinks nothing, so
"nothing deleted" alone would pass against it; each refusal test also asserts the exact refusal type
(and, for H-42, its status), and the run still being there.

Every test here is red on the skeleton's `NotImplementedError` body; none is green on arrival.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from tailorcraft.application.retention.erase_history_entry import EraseHistoryEntry
from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRun
from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.retention.errors import HistoryEntryInProgress
from tailorcraft.domain.retention.value_objects import (
    DeletedHistoryEntry,
    HistoryEntryErasureReport,
)
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable
from tailorcraft.domain.tailoring.errors import (
    TailoringRunNotFound,
    TailoringRunNotOwnedByUser,
)
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.settings import Settings
from tests.integration.fakes import (
    FakeExportJobRepository,
    FakeGuestSessionRepository,
    FakeJobPostingRepository,
    FakeTailoringRunRepository,
    FakeUserRepository,
    InMemoryFileStore,
    RecordingHistoryEntryData,
    create_active_session,
)
from tests.integration.owners import (
    failed_run,
    pasted_posting,
    queued_export,
    queued_run,
    ready_export,
    running_run,
    seed_user,
    succeeded_run,
)

_APPLICATION_LOGGER = "tailorcraft.application"


class _OrderRecordingRuns(FakeTailoringRunRepository):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    async def get(self, run_id: TailoringRunId) -> TailoringRun:
        run = await super().get(run_id)
        self._order.append("get")
        return run


class _OrderRecordingEntries(RecordingHistoryEntryData):
    def __init__(
        self,
        order: list[str],
        runs: FakeTailoringRunRepository,
        jobs: FakeExportJobRepository,
        postings: FakeJobPostingRepository,
        *,
        tracked_application_deleted: bool = False,
    ) -> None:
        super().__init__(
            runs, jobs, postings, tracked_application_deleted=tracked_application_deleted
        )
        self._order = order

    async def delete_history_entry(
        self, user_id: UserId, run_id: UUID
    ) -> DeletedHistoryEntry | None:
        self._order.append("delete_history_entry")
        return await super().delete_history_entry(user_id, run_id)


class _OrderRecordingFiles(InMemoryFileStore):
    def __init__(
        self,
        order: list[str],
        *,
        fail_delete: Exception | None,
        fail_delete_keys: set[str] | None,
    ) -> None:
        super().__init__(fail_delete=fail_delete, fail_delete_keys=fail_delete_keys)
        self._order = order

    async def delete(self, ref: FileRef) -> None:
        self._order.append("unlink")
        await super().delete(ref)


class _RunVanishesAfterLoad(FakeTailoringRunRepository):
    """The run is loaded (authorized, finished) and then deleted by a concurrent erasure before this
    call's `delete_history_entry` runs — H-44's loser."""

    async def get(self, run_id: TailoringRunId) -> TailoringRun:
        run = await super().get(run_id)
        self.discard(run_id)
        return run


class _World:
    def __init__(
        self,
        clock: FixedClock,
        *,
        runs: FakeTailoringRunRepository | None = None,
        fail_delete: Exception | None = None,
        fail_delete_keys: set[str] | None = None,
        tracked_application_deleted: bool = False,
    ) -> None:
        self.clock = clock
        self.order: list[str] = []
        self.sessions = FakeGuestSessionRepository()
        self.users = FakeUserRepository()
        self.runs = runs if runs is not None else _OrderRecordingRuns(self.order)
        self.jobs = FakeExportJobRepository()
        self.postings = FakeJobPostingRepository()
        self.entries = _OrderRecordingEntries(
            self.order,
            self.runs,
            self.jobs,
            self.postings,
            tracked_application_deleted=tracked_application_deleted,
        )
        self.files = _OrderRecordingFiles(
            self.order, fail_delete=fail_delete, fail_delete_keys=fail_delete_keys
        )
        self.earlier = clock.now() - timedelta(hours=1)

    def use_case(self) -> EraseHistoryEntry:
        return EraseHistoryEntry(
            GetTailoringRun(self.runs, self.sessions, self.users, self.clock),
            self.entries,
            self.files,
        )

    async def user(self, email: str = "alex@example.com") -> UserOwner:
        return await seed_user(self.users, email, self.earlier)

    async def entry(
        self,
        owner: Owner,
        build: Callable[..., TailoringRun] = succeeded_run,
        *,
        exports: tuple[ExportFormat, ...] = (ExportFormat.PDF, ExportFormat.DOCX),
    ) -> tuple[TailoringRun, list[FileRef]]:
        """A run with its own posting and one export job per format, each with its file stored."""
        posting = pasted_posting(owner, self.earlier)
        await self.postings.add(posting)
        run = build(owner, self.earlier, job_posting_id=posting.id)
        await self.runs.add(run)
        refs: list[FileRef] = []
        for export_format in exports:
            job = ready_export(owner, run, self.earlier, format=export_format)
            await self.jobs.add(job)
            await self.files.put(job.storage_ref, b"rendered bytes")
            refs.append(job.storage_ref)
        return run, refs


# --- The happy path, and its order -----------------------------------------------------------------


async def test_a_finished_entry_is_deleted_rows_first_then_each_file(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    run, refs = await w.entry(user)

    report = await w.use_case()(user.user_id, run.id)

    assert report == HistoryEntryErasureReport(
        export_jobs=2, files_unlinked=2, unlink_failures=(), posting_deleted=True
    )
    assert w.order == ["get", "delete_history_entry", "unlink", "unlink"]
    assert w.entries.calls == [(user.user_id, run.id.value)]
    assert set(w.files.delete_calls) == set(refs)
    assert all(ref.key not in w.files.data for ref in refs)
    assert await w.runs.find(run.id) is None


async def test_the_report_carries_tracked_application_deleted_true_from_the_port(
    clock: FixedClock,
) -> None:
    """AC-13: the flag the port reports reaches the use case's report (planted True, so a use case
    that drops it and leaves the default False fails here)."""
    w = _World(clock, tracked_application_deleted=True)
    user = await w.user()
    run, _ = await w.entry(user)

    report = await w.use_case()(user.user_id, run.id)

    assert report.tracked_application_deleted is True
    assert report.export_jobs == 2  # the rest of the report is the port's too


async def test_the_report_says_no_tracked_application_when_the_port_says_none(
    clock: FixedClock,
) -> None:
    """AC-13's pair: an entry with no tracked application reports False."""
    w = _World(clock, tracked_application_deleted=False)
    user = await w.user()
    run, _ = await w.entry(user)

    report = await w.use_case()(user.user_id, run.id)

    assert report.tracked_application_deleted is False
    assert report.export_jobs == 2


@pytest.mark.parametrize("build", [succeeded_run, failed_run], ids=["succeeded", "failed"])
async def test_both_terminal_statuses_are_deletable(
    build: Callable[..., TailoringRun], clock: FixedClock
) -> None:
    w = _World(clock)
    user = await w.user()
    run, _ = await w.entry(user, build, exports=())

    report = await w.use_case()(user.user_id, run.id)

    assert report.export_jobs == 0
    assert await w.runs.find(run.id) is None


async def test_every_export_key_is_unlinked_including_a_job_that_never_finished(
    clock: FixedClock,
) -> None:
    """The keys come back from the deletion itself, derived from `(id, format)` — so a `queued`
    job's key (bytes possibly written by a worker mid-render) is unlinked too (ADR-0018 decision 3,
    reused)."""
    w = _World(clock)
    user = await w.user()
    run, refs = await w.entry(user, exports=(ExportFormat.PDF,))
    unfinished = queued_export(user, run, w.earlier, format=ExportFormat.DOCX)
    await w.jobs.add(unfinished)

    report = await w.use_case()(user.user_id, run.id)

    assert report.export_jobs == 2
    assert set(w.files.delete_calls) == {*refs, unfinished.storage_ref}


async def test_a_posting_another_run_still_uses_is_kept_and_reported_kept(
    clock: FixedClock,
) -> None:
    w = _World(clock)
    user = await w.user()
    run, _ = await w.entry(user, exports=())
    sibling = succeeded_run(user, w.earlier, job_posting_id=run.job_posting_id)
    await w.runs.add(sibling)

    report = await w.use_case()(user.user_id, run.id)

    assert report.posting_deleted is False
    assert await w.postings.get(run.job_posting_id) is not None
    assert await w.runs.find(run.id) is None


# --- H-42: an entry in progress --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "status"),
    [
        pytest.param(queued_run, "queued", id="queued"),
        pytest.param(running_run, "running", id="running"),
    ],
)
async def test_an_entry_in_progress_is_refused_with_nothing_deleted(
    build: Callable[..., TailoringRun], status: str, clock: FixedClock
) -> None:
    w = _World(clock)
    user = await w.user()
    run, _ = await w.entry(user, build, exports=())

    with pytest.raises(HistoryEntryInProgress) as exc_info:
        await w.use_case()(user.user_id, run.id)

    assert type(exc_info.value) is HistoryEntryInProgress
    assert exc_info.value.status == status
    assert w.entries.calls == []
    assert w.files.delete_calls == []
    assert await w.runs.find(run.id) is not None


# --- H-31: not the user's --------------------------------------------------------------------------


@pytest.mark.parametrize("owner_kind", ["other_user", "guest"])
async def test_someone_elses_entry_is_not_found_and_nothing_is_deleted(
    owner_kind: str, clock: FixedClock
) -> None:
    w = _World(clock)
    user = await w.user()
    if owner_kind == "guest":
        session = await create_active_session(w.sessions, clock)
        owner: Owner = GuestOwner(session.id)
    else:
        owner = await w.user("b@example.com")
    run, refs = await w.entry(owner)

    with pytest.raises(TailoringRunNotFound) as exc_info:
        await w.use_case()(user.user_id, run.id)

    assert type(exc_info.value) is TailoringRunNotFound
    assert type(exc_info.value.__cause__) is TailoringRunNotOwnedByUser
    assert w.entries.calls == []
    assert w.files.delete_calls == []
    assert await w.runs.find(run.id) is not None
    assert all(ref.key in w.files.data for ref in refs)


async def test_a_nonexistent_entry_is_not_found(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()

    with pytest.raises(TailoringRunNotFound) as exc_info:
        await w.use_case()(user.user_id, TailoringRunId(value=uuid4()))

    assert type(exc_info.value) is TailoringRunNotFound
    assert type(exc_info.value.__cause__) is not TailoringRunNotOwnedByUser
    assert w.entries.calls == []


async def test_an_erased_user_is_refused_before_its_entry_is_read(clock: FixedClock) -> None:
    w = _World(clock)
    await w.user()
    erased = UserOwner(UserId(value=uuid4()))
    run, _ = await w.entry(erased)

    with pytest.raises(UserNotFound) as exc_info:
        await w.use_case()(erased.user_id, run.id)

    assert type(exc_info.value) is UserNotFound
    assert w.order == []
    assert await w.runs.find(run.id) is not None


# --- H-44: second / concurrent deletion ------------------------------------------------------------


async def test_a_second_deletion_is_not_found_and_unlinks_nothing(clock: FixedClock) -> None:
    w = _World(clock)
    user = await w.user()
    run, refs = await w.entry(user)
    first = await w.use_case()(user.user_id, run.id)
    assert first.files_unlinked == 2
    assert set(w.files.delete_calls) == set(refs)
    unlinks_after_first = list(w.files.delete_calls)

    with pytest.raises(TailoringRunNotFound) as exc_info:
        await w.use_case()(user.user_id, run.id)

    assert type(exc_info.value) is TailoringRunNotFound
    assert w.files.delete_calls == unlinks_after_first


async def test_the_loser_of_a_concurrent_deletion_is_not_found_and_unlinks_nothing(
    clock: FixedClock,
) -> None:
    """The run was loaded and authorized, then a concurrent deletion took it; this call's
    `delete_history_entry` finds no row and returns `None` → `TailoringRunNotFound`, and the export
    files — still on disk, the winner's to unlink — are left alone."""
    w = _World(clock, runs=_RunVanishesAfterLoad())
    user = await w.user()
    run, refs = await w.entry(user)

    with pytest.raises(TailoringRunNotFound) as exc_info:
        await w.use_case()(user.user_id, run.id)

    assert type(exc_info.value) is TailoringRunNotFound
    assert w.entries.calls == [(user.user_id, run.id.value)]  # it did ask
    assert w.files.delete_calls == []
    assert all(ref.key in w.files.data for ref in refs)


# --- Failures are returned, never raised, never logged ---------------------------------------------


async def test_an_unlink_failure_is_returned_as_a_type_name_and_the_rest_still_go(
    clock: FixedClock,
) -> None:
    failing_keys: set[str] = set()  # filled once the job ids exist; the store holds this set
    w = _World(
        clock,
        fail_delete=FileStoreUnavailable("simulated EIO at /uploads/secret/path"),
        fail_delete_keys=failing_keys,
    )
    user = await w.user()
    run, refs = await w.entry(user)
    failing = refs[0]
    failing_keys.add(failing.key)

    report = await w.use_case()(user.user_id, run.id)

    assert report.unlink_failures == ("FileStoreUnavailable",)
    assert report.files_unlinked == 1
    assert report.export_jobs == 2
    assert w.files.delete_calls.count(failing) == 1
    assert refs[1].key not in w.files.data


async def test_the_use_case_writes_no_log_record(
    settings: Settings, caplog: pytest.LogCaptureFixture, clock: FixedClock
) -> None:
    """AC-14: **zero** records from `tailorcraft.application`, over a run that had something worth
    logging (an unlink failure). The probe first proves this capture *can* see an
    application-namespace record — otherwise "nothing captured" would be a statement about the
    harness (1.6's Alembic `fileConfig` lesson)."""
    configure_logging(settings)
    w = _World(clock, fail_delete=FileStoreUnavailable("simulated EIO"))
    user = await w.user()
    run, _ = await w.entry(user)

    with caplog.at_level(logging.DEBUG, logger=_APPLICATION_LOGGER):
        logging.getLogger(f"{_APPLICATION_LOGGER}.probe").warning("probe")
        assert [r.name for r in caplog.records] == [f"{_APPLICATION_LOGGER}.probe"]
        caplog.clear()

        report = await w.use_case()(user.user_id, run.id)

    assert report.unlink_failures == ("FileStoreUnavailable", "FileStoreUnavailable")
    assert [r for r in caplog.records if r.name.startswith(_APPLICATION_LOGGER)] == []
