"""In-memory fakes shared by the `integration` suite's application-layer tests.

Lifted out of `tests/integration/intake/test_upload_base_cv.py` (T9) rather than duplicated,
because a second hand-written copy of `FakeBaseCvRepository` or `FakeGuestSessionRepository` is
exactly how a test suite starts lying: the two copies drift, one gets a bug fixed and the other
does not, and nobody notices until a test that "should" catch a regression passes against the
stale fake. Every class here still satisfies its Protocol exactly (`domain/intake/ports.py`,
`domain/identity/ports.py`, `domain/shared/files.py`, `domain/shared/events.py`) — moving a fake to
a shared module changes nothing about what it does.

Used by:
- `tests/integration/intake/test_upload_base_cv.py` (T9/T10 — `UploadBaseCv`)
- `tests/integration/intake/test_read_base_cvs.py` (T12 — `GetBaseCvForSession`,
  `ListBaseCvsForSession`)
- `tests/integration/identity/test_start_guest_session.py` (T12 — `StartGuestSession`)
- `tests/integration/posting/test_capture_job_posting.py` (T9/T10 — `CaptureJobPosting`)
- `tests/integration/posting/test_read_job_postings.py` (T12/T13 — `GetJobPostingForSession`,
  `ListJobPostingsForSession`)
- `tests/integration/tailoring/test_request_tailoring_run.py` (T9/T10 — `RequestTailoringRun`).
  `FakeLlm` and `FakeTailoringQueue` are added in this same commit even though this file does not
  yet exercise them — T12 (`ExecuteTailoringRun`) and the T29 API tests need both, and a fake added
  in the commit that first uses its sibling port is how this file avoids ever growing a second,
  drifting copy of one.
- `tests/integration/tailoring/test_execute_tailoring_run.py` (T12 — `ExecuteTailoringRun`).
  `FakeTailoringRunRepository.save_calls` and `FakeLlm.on_call` are added in this commit: T12 needs
  to observe the repository's state at the exact moment the LLM is invoked, to prove `running` is
  saved before the call rather than after (technical-plan.md's "Step 4 — two commits").
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from tailorcraft.domain.identity.errors import GuestSessionNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound, CvExtractionFailed
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType, ExtractedText
from tailorcraft.domain.posting.errors import JobPostingFetchFailed, JobPostingNotFound
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import (
    FetchedPosting,
    JobPostingId,
    JobPostingText,
    SourceUrl,
)
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable
from tailorcraft.domain.tailoring.errors import (
    TailoringFailed,
    TailoringNotQueued,
    TailoringRunNotFound,
)
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    TailoredDraft,
    TailoringRunId,
    TailoringRunStatus,
)
from tailorcraft.infrastructure.clock import FixedClock

# A `NULL`-`started_at` stand-in for `FakeTailoringRunRepository.list_stale_running`'s sort key:
# older than any real instant, so a `None` sorts first exactly as `NULLS FIRST` would in SQL.
_EPOCH = datetime.min.replace(tzinfo=UTC)


# --- Fakes -------------------------------------------------------------------------------------


class FakeBaseCvRepository:
    """In-memory `BaseCvRepository`."""

    def __init__(self) -> None:
        self._by_id: dict[BaseCvId, BaseCv] = {}

    def next_identity(self) -> BaseCvId:
        return BaseCvId(value=uuid4())

    async def add(self, cv: BaseCv) -> None:
        self._by_id[cv.id] = cv

    async def get(self, cv_id: BaseCvId) -> BaseCv:
        try:
            return self._by_id[cv_id]
        except KeyError:
            raise BaseCvNotFound(str(cv_id)) from None

    async def list_for_session(self, sid: GuestSessionId) -> Sequence[BaseCv]:
        return [cv for cv in self._by_id.values() if cv.guest_session_id == sid]

    async def count_for_session(self, sid: GuestSessionId) -> int:
        return len(await self.list_for_session(sid))

    def all(self) -> list[BaseCv]:
        """Test-only inspection, not part of `BaseCvRepository`."""
        return list(self._by_id.values())


class FakeGuestSessionRepository:
    """In-memory `GuestSessionRepository`."""

    def __init__(self) -> None:
        self._by_id: dict[GuestSessionId, GuestSession] = {}

    def next_identity(self) -> GuestSessionId:
        return GuestSessionId(value=uuid4())

    async def add(self, session: GuestSession) -> None:
        self._by_id[session.id] = session

    async def get(self, session_id: GuestSessionId) -> GuestSession:
        try:
            return self._by_id[session_id]
        except KeyError:
            raise GuestSessionNotFound(str(session_id)) from None

    async def find_by_token_hash(self, token_hash: str) -> GuestSession | None:
        for session in self._by_id.values():
            if session.token_hash == token_hash:
                return session
        return None


class FakeJobPostingRepository:
    """In-memory `JobPostingRepository`. Same shape as `FakeBaseCvRepository`, deliberately: the two
    ports are the same shape (`domain/posting/ports.py`'s docstring says so explicitly), so the fakes
    are too."""

    def __init__(self) -> None:
        self._by_id: dict[JobPostingId, JobPosting] = {}

    def next_identity(self) -> JobPostingId:
        return JobPostingId(value=uuid4())

    async def add(self, posting: JobPosting) -> None:
        self._by_id[posting.id] = posting

    async def get(self, posting_id: JobPostingId) -> JobPosting:
        try:
            return self._by_id[posting_id]
        except KeyError:
            raise JobPostingNotFound(str(posting_id)) from None

    async def list_for_session(self, sid: GuestSessionId) -> Sequence[JobPosting]:
        return [posting for posting in self._by_id.values() if posting.guest_session_id == sid]

    async def count_for_session(self, sid: GuestSessionId) -> int:
        return len(await self.list_for_session(sid))

    def all(self) -> list[JobPosting]:
        """Test-only inspection, not part of `JobPostingRepository`."""
        return list(self._by_id.values())


class InMemoryFileStore:
    """In-memory `FileStorePort`, backed by a plain dict.

    Optionally takes a `FakeBaseCvRepository` the use case is also given, purely so a test can
    prove the **ordering** the technical plan requires (step 5 before step 6/8, ADR-0006 §2): the
    file is written while the repository is still empty. `repo_size_at_put` snapshots
    `len(repo.all())` at the moment `put` runs, so the assertion is positive ("the repo held zero
    rows when the file landed") rather than only checking the end state.
    """

    def __init__(self, repo: FakeBaseCvRepository | None = None) -> None:
        self._repo = repo
        self.data: dict[str, bytes] = {}
        self.repo_size_at_put: int | None = None

    async def put(self, ref: FileRef, data: bytes) -> None:
        if self._repo is not None:
            self.repo_size_at_put = len(self._repo.all())
        self.data[ref.key] = data

    async def get(self, ref: FileRef) -> bytes:
        return self.data[ref.key]

    async def delete(self, ref: FileRef) -> None:
        self.data.pop(ref.key, None)


class AlwaysFailingFileStore:
    """`FileStorePort` that fails every write, simulating F-14 (`ENOSPC` / `EACCES`)."""

    async def put(self, ref: FileRef, data: bytes) -> None:
        raise FileStoreUnavailable("simulated storage failure")

    async def get(self, ref: FileRef) -> bytes:
        raise AssertionError("get() should not be reached in this scenario")

    async def delete(self, ref: FileRef) -> None:
        raise AssertionError("delete() should not be reached in this scenario")


class FakeExtractor:
    """`CvTextExtractorPort` that either returns a fixed `ExtractedText` or raises a fixed
    `CvExtractionFailed` — one instance per test, configured with exactly the outcome that test is
    about."""

    def __init__(self, outcome: ExtractedText | CvExtractionFailed) -> None:
        self._outcome = outcome

    async def extract(self, content_type: CvContentType, data: bytes) -> ExtractedText:
        if isinstance(self._outcome, CvExtractionFailed):
            raise self._outcome
        return self._outcome


class FakeJobPostingFetcher:
    """`JobPostingFetcherPort` that either returns a fixed `FetchedPosting` or raises a fixed
    `JobPostingFetchFailed` — one instance per test, configured with exactly the outcome that test
    is about. Mirrors `FakeExtractor`'s shape, with one addition: `calls` counts invocations, so a
    paste-path test can assert the fetcher was never reached (`fetcher.calls == 0`) rather than only
    trusting that a value it never used happened not to matter."""

    def __init__(self, outcome: FetchedPosting | JobPostingFetchFailed) -> None:
        self._outcome = outcome
        self.calls = 0

    async def fetch(self, url: SourceUrl) -> FetchedPosting:
        self.calls += 1
        if isinstance(self._outcome, JobPostingFetchFailed):
            raise self._outcome
        return self._outcome


class FakeTailoringRunRepository:
    """In-memory `TailoringRunRepository`.

    Same shape as `FakeBaseCvRepository` and `FakeJobPostingRepository` — `next_identity`, `add`,
    `get`-raises, `list_for_session`, `count_for_session` — extended with the two methods this port
    adds because `TailoringRun` is the codebase's first aggregate that is loaded, mutated and
    persisted by a second process: `save` (an unconditional overwrite here, same as `add`, since an
    in-memory dict has no concept of "the row already existed") and `find` (the worker's
    `get`-returns-`None` counterpart, so a fake exercising `ExecuteTailoringRun` can hand back
    `MISSING` for a purged id without raising).

    `save_calls` records the `status` recorded by every `save()` call, in order — added for T12's
    "`running` is committed before the LLM is called" test (technical-plan.md's "Step 4 — two
    commits"). That test needs to observe the repository's state at the exact moment `LlmPort.tailor`
    is invoked (via `FakeLlm.on_call`, below), and a plain end-state assertion cannot do that: by the
    time the use case returns, `save` has already been called again with the terminal outcome, so
    only a call log — not the current row — can prove `running` was saved *before* the model was
    ever asked. A count alone would not do either, since `1` is consistent with "saved before the
    call" and "saved after, coincidentally also once"; recording the status distinguishes them.

    `list_stale_running`, added for V5b (the stale-run sweep, G-25'), is **not yet a member of
    `TailoringRunRepository`** — the skeleton commit measured that adding it breaks mypy in six
    files this layer may not touch, so it lands on the Protocol only once every implementer already
    has it. Structural typing makes that order safe: this class satisfies the port either way. The
    contract, matching the SQL adapter's once it exists: `RUNNING` rows whose `started_at` is before
    `started_before`, **or** `started_at is None` — the same fold `TailoringRun.is_stale` makes,
    expressed as a filter — oldest first with a `NULL` counted as oldest (`NULLS FIRST`), ties on
    `started_at` broken by id, at most `limit`, no locking.
    """

    def __init__(self) -> None:
        self._by_id: dict[TailoringRunId, TailoringRun] = {}
        self.save_calls: list[TailoringRunStatus] = []

    def next_identity(self) -> TailoringRunId:
        return TailoringRunId(value=uuid4())

    async def add(self, run: TailoringRun) -> None:
        self._by_id[run.id] = run

    async def save(self, run: TailoringRun) -> None:
        self.save_calls.append(run.status)
        self._by_id[run.id] = run

    async def get(self, run_id: TailoringRunId) -> TailoringRun:
        try:
            return self._by_id[run_id]
        except KeyError:
            raise TailoringRunNotFound(str(run_id)) from None

    async def find(self, run_id: TailoringRunId) -> TailoringRun | None:
        return self._by_id.get(run_id)

    async def list_for_session(self, sid: GuestSessionId) -> Sequence[TailoringRun]:
        runs = [run for run in self._by_id.values() if run.guest_session_id == sid]
        return sorted(runs, key=lambda run: run.requested_at, reverse=True)

    async def count_for_session(self, sid: GuestSessionId) -> int:
        return len([run for run in self._by_id.values() if run.guest_session_id == sid])

    async def find_active_for_session(self, sid: GuestSessionId) -> TailoringRun | None:
        active_statuses = (TailoringRunStatus.QUEUED, TailoringRunStatus.RUNNING)
        for run in self._by_id.values():
            if run.guest_session_id == sid and run.status in active_statuses:
                return run
        return None

    async def list_stale_running(
        self, started_before: datetime, limit: int
    ) -> Sequence[TailoringRun]:
        """`AbandonStaleTailoringRuns`' lookup (V5b, G-25'). See the class docstring for the
        contract this implements ahead of the Protocol gaining the member."""
        candidates = [
            run
            for run in self._by_id.values()
            if run.status is TailoringRunStatus.RUNNING
            and (run.started_at is None or run.started_at < started_before)
        ]

        def _sort_key(run: TailoringRun) -> tuple[bool, datetime, UUID]:
            # `started_at is None` sorts first (`False < True`); ties on `started_at` break on id.
            return (run.started_at is not None, run.started_at or _EPOCH, run.id.value)

        candidates.sort(key=_sort_key)
        return candidates[:limit]

    def all(self) -> list[TailoringRun]:
        """Test-only inspection, not part of `TailoringRunRepository`."""
        return list(self._by_id.values())


class FakeLlm:
    """`LlmPort` that either returns a fixed `TailoredDraft` or raises a fixed `TailoringFailed` —
    one instance per test, configured with exactly the outcome that test is about. Same constructor
    shape as `FakeExtractor` and `FakeJobPostingFetcher` on purpose, so all three fakes read alike.

    `calls` records the exact `(cv, posting)` argument pairs `tailor` was invoked with, not merely a
    count: that is what lets a test count attempts (AC-8/AC-10) and assert the port received the CV
    text and the posting text **and nothing else** (AC-24) — a count alone could not distinguish
    "called once with the right text" from "called once with someone else's".

    `delay_seconds` sleeps before producing the configured outcome, on every call. It is not used by
    `RequestTailoringRun`'s tests (this port is never reached from there) but exists here rather than
    being bolted on later, so that `ExecuteTailoringRun`'s per-attempt and total-deadline timeout
    tests can drive this fake past a configured budget without a real network call.

    `on_call`, added for T12's "`running` is committed before the LLM is called" test, is a
    synchronous hook invoked the instant `tailor()` starts — before the delay, before the outcome is
    produced or raised. A test wires it to snapshot `FakeTailoringRunRepository.save_calls` at that
    exact moment, which is what turns "was the run already saved as `running` when the model was
    asked?" into a plain list-equality assertion rather than a guess based on the end state.

    `aclose` is a recording no-op, added at verify-round-1 (V4): `tasks/container.py::
    tailoring_use_case` now closes whatever `GeminiLlm(settings)` produced in its own `finally`, and
    every test that monkeypatches that factory to return a `FakeLlm` runs that same `finally` — so
    the fake needs the method just to keep those tests from raising `AttributeError`, and recording
    that it ran is what lets a test assert the container actually closed its adapter rather than
    merely surviving the call. Not on `LlmPort` itself: adapter lifecycle is not domain language, and
    a port that grew `aclose` would make every fake implement a lifecycle the business model has no
    word for (`GeminiLlm.aclose`'s own docstring).
    """

    def __init__(
        self,
        outcome: TailoredDraft | TailoringFailed,
        *,
        delay_seconds: float = 0.0,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self._outcome = outcome
        self._delay_seconds = delay_seconds
        self._on_call = on_call
        self.calls: list[tuple[ExtractedText, JobPostingText]] = []
        self.closed = False

    async def tailor(self, cv: ExtractedText, posting: JobPostingText) -> TailoredDraft:
        self.calls.append((cv, posting))
        if self._on_call is not None:
            self._on_call()
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if isinstance(self._outcome, TailoringFailed):
            raise self._outcome
        return self._outcome

    async def aclose(self) -> None:
        self.closed = True


class FakeTailoringQueue:
    """`TailoringQueuePort` that records every id it was asked to enqueue, or raises a fixed
    `TailoringNotQueued` instead — one instance per test, configured with exactly the outcome that
    test is about, mirroring `FakeExtractor`'s and `FakeJobPostingFetcher`'s shape. The default
    (`outcome=None`) is the broker accepting the publish; passing a `TailoringNotQueued` instance is
    G-14's broker-down variant.

    Not exercised by `RequestTailoringRun`'s tests — that use case never enqueues, per its own
    docstring — but added in this commit for the same reason `FakeLlm` is: T12 and the T29 API tests
    need it, and this is where a fake belongs so it never grows a second copy.
    """

    def __init__(self, outcome: TailoringNotQueued | None = None) -> None:
        self._outcome = outcome
        self.enqueued: list[TailoringRunId] = []

    async def enqueue(self, run_id: TailoringRunId) -> None:
        if self._outcome is not None:
            raise self._outcome
        self.enqueued.append(run_id)


class _HasAll(Protocol):
    """Structural type for "a fake repository with a test-only `.all()` inspector" — satisfied by
    both `FakeBaseCvRepository` and `FakeJobPostingRepository` without either needing to share a
    base class with the other (CLAUDE.md: shared shape is not shared behaviour; this is a
    test-fixture convenience typed narrowly enough to stay honest about that)."""

    def all(self) -> Sequence[object]: ...


class RecordingEventPublisher:
    """`EventPublisherPort` that records what it was handed, for AC-13-style assertions on the
    published events' field sets and content.

    Optionally takes a repository-like fake exposing `.all()`, purely so a test can prove
    **publish-after-save** ordering — the same technique `InMemoryFileStore(repo=cvs)` uses for
    T9's file-before-row proof, aimed the other way. `repo_size_at_first_publish` snapshots
    `len(repo.all())` at the moment `publish` is first called, so an assertion that it is nonzero is
    positive evidence the save already happened by then, rather than only an end-state check that
    would pass even if publish ran first.
    """

    def __init__(self, repo: _HasAll | None = None) -> None:
        self._repo = repo
        self.published: list[DomainEvent] = []
        self.repo_size_at_first_publish: int | None = None

    async def publish(self, *events: DomainEvent) -> None:
        if self._repo is not None and self.repo_size_at_first_publish is None:
            self.repo_size_at_first_publish = len(self._repo.all())
        self.published.extend(events)


# --- Shared test helpers -------------------------------------------------------------------------


async def create_active_session(
    sessions: FakeGuestSessionRepository,
    clock: FixedClock,
    *,
    token_hash: str = "a" * 64,
    ttl_hours: int = 24,
) -> GuestSession:
    """Start and persist a `GuestSession` that has not expired at `clock.now()`.

    `token_hash` defaults to a fixed dummy hash — fine when a test only ever needs one session, but
    a test asserting the T12 authorization rule (two sessions, each owning some CVs) must pass a
    distinct `token_hash` per session, since `FakeGuestSessionRepository.find_by_token_hash` looks
    sessions up by that value.
    """
    session = GuestSession.start(
        id=sessions.next_identity(),
        token_hash=token_hash,
        at=clock.now(),
        ttl_hours=ttl_hours,
    )
    await sessions.add(session)
    return session
