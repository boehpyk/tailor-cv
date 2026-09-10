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
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import uuid4

from tailorcraft.domain.identity.errors import GuestSessionNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound, CvExtractionFailed
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType, ExtractedText
from tailorcraft.domain.posting.errors import JobPostingFetchFailed, JobPostingNotFound
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import FetchedPosting, JobPostingId, SourceUrl
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable
from tailorcraft.infrastructure.clock import FixedClock

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
