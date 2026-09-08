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
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

from tailorcraft.domain.identity.errors import GuestSessionNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound, CvExtractionFailed
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType, ExtractedText
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


class RecordingEventPublisher:
    """`EventPublisherPort` that records what it was handed, for AC-13-style assertions on the
    published events' field sets and content."""

    def __init__(self) -> None:
        self.published: list[DomainEvent] = []

    async def publish(self, *events: DomainEvent) -> None:
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
