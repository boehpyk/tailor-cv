"""Application tests for `UploadBaseCv` (T9, RED).

**Why these fakes, not a real Postgres:** the technical plan's Test plan says this suite runs
"against the real test DB with transactional rollback", and it will — once T14-T17 land the
imperative mappings, the repositories and the first Alembic migration. As of this commit none of
that exists (`infrastructure/persistence/mapping/` has no mapping modules, and there is no
migration to bring `tailorcraft_test` to head), so a test that imported `conftest.py`'s `session` /
`engine` fixtures would fail for a reason that has nothing to do with `UploadBaseCv`. Writing the
red honestly means testing the use case against the ports it actually depends on: in-memory fakes
of `BaseCvRepository`, `GuestSessionRepository` and `FileStorePort`, defined below, each satisfying
its Protocol exactly. **T28** is where the real persistence round-trip gets its own test, once the
repositories exist to round-trip through.

The aggregates are not faked — `BaseCv` and `GuestSession` are the real domain classes.

Every assertion below states what `UploadBaseCv.__call__` should do per technical-plan.md's
"Flow" section and feature-spec.md's failure contract, never what the (currently `NotImplementedError`)
code was observed doing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.intake.upload_base_cv import (
    UploadBaseCv,
    UploadBaseCvCommand,
    UploadBaseCvResult,
)
from tailorcraft.domain.identity.errors import GuestSessionExpired, GuestSessionNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import (
    BaseCvNotFound,
    CorruptCvFile,
    CvExtractionFailed,
    CvHasNoTextLayer,
    CvHasTooManyPages,
    CvTextTooShort,
    EncryptedCvFile,
    TooManyBaseCvs,
)
from tailorcraft.domain.intake.errors import (
    CvExtractionTimedOut as CvExtractionTimedOutError,
)
from tailorcraft.domain.intake.events import (
    BaseCvExtractionFailed,
    BaseCvTextExtracted,
    BaseCvUploaded,
)
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    BaseCvStatus,
    CvContentType,
    ExtractedText,
    ExtractionFailureReason,
    OriginalFilename,
)
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable
from tailorcraft.infrastructure.clock import FixedClock

# --- Fakes -------------------------------------------------------------------------------------
# Each satisfies its Protocol (domain/intake/ports.py, domain/identity/ports.py,
# domain/shared/files.py, domain/shared/events.py) exactly. Stand-ins for T14-T17's real adapters —
# see the module docstring.


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

    Optionally takes the `FakeBaseCvRepository` the use case is also given, purely so a test can
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


# --- Test helpers --------------------------------------------------------------------------------


async def _active_session(sessions: FakeGuestSessionRepository, clock: FixedClock) -> GuestSession:
    session = GuestSession.start(
        id=sessions.next_identity(),
        token_hash="a" * 64,
        at=clock.now(),
        ttl_hours=24,
    )
    await sessions.add(session)
    return session


def _command(
    session_id: GuestSessionId,
    *,
    filename: str = "cv.pdf",
    content_type: CvContentType = CvContentType.PDF,
    content: bytes = b"content bytes for a fake upload, not a real PDF",
) -> UploadBaseCvCommand:
    return UploadBaseCvCommand(
        guest_session_id=session_id,
        original_filename=OriginalFilename(filename),
        content_type=content_type,
        content=content,
    )


# --- 1. Happy path -------------------------------------------------------------------------------


async def test_happy_path_stores_extracts_and_publishes(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await _active_session(sessions, clock)
    cvs = FakeBaseCvRepository()
    files = InMemoryFileStore()
    text = ExtractedText("word " * 200)
    extractor = FakeExtractor(outcome=text)
    events = RecordingEventPublisher()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock)

    cmd = _command(session.id)
    result = await use_case(cmd)

    assert isinstance(result, UploadBaseCvResult)
    assert result.status is BaseCvStatus.EXTRACTED
    assert result.character_count == text.character_count
    assert result.failure_reason is None

    # the file is in the store
    ref = FileRef.for_base_cv(result.base_cv_id, CvContentType.PDF)
    assert await files.get(ref) == cmd.content

    # the CV is in the repository
    stored = await cvs.get(result.base_cv_id)
    assert stored.status is BaseCvStatus.EXTRACTED
    assert stored.extracted_text == text

    # BaseCvUploaded + BaseCvTextExtracted were published, in that order
    event_types = [type(event) for event in events.published]
    assert event_types == [BaseCvUploaded, BaseCvTextExtracted]

    # never the CV text, in any event
    for event in events.published:
        assert text.value not in repr(event)


# --- 2. Failure-contract rows F-7...F-12 ----------------------------------------------------------

_EXTRACTION_FAILURE_CASES = [
    pytest.param(EncryptedCvFile(), ExtractionFailureReason.ENCRYPTED, id="F-7-encrypted"),
    pytest.param(CorruptCvFile(), ExtractionFailureReason.CORRUPT, id="F-8-corrupt"),
    pytest.param(CvHasNoTextLayer(), ExtractionFailureReason.NO_TEXT_LAYER, id="F-9-no_text_layer"),
    pytest.param(CvTextTooShort(), ExtractionFailureReason.TOO_SHORT, id="F-10-too_short"),
    pytest.param(
        CvHasTooManyPages(), ExtractionFailureReason.TOO_MANY_PAGES, id="F-11-too_many_pages"
    ),
    pytest.param(
        CvExtractionTimedOutError(), ExtractionFailureReason.EXTRACTOR_ERROR, id="F-12-timed_out"
    ),
]


@pytest.mark.parametrize(("exc", "expected_reason"), _EXTRACTION_FAILURE_CASES)
async def test_extraction_failure_is_recorded_as_a_state_and_does_not_propagate(
    clock: FixedClock, exc: CvExtractionFailed, expected_reason: ExtractionFailureReason
) -> None:
    """ADR-0004's rule, and the single most important assertion in this file: calling `use_case()`
    below must return normally. If `CvExtractionFailed` escaped the use case, this test would fail
    with that exception rather than reach any assertion."""
    sessions = FakeGuestSessionRepository()
    session = await _active_session(sessions, clock)
    cvs = FakeBaseCvRepository()
    files = InMemoryFileStore()
    extractor = FakeExtractor(outcome=exc)
    events = RecordingEventPublisher()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock)

    cmd = _command(session.id)
    result = await use_case(cmd)  # must not raise

    assert result.status is BaseCvStatus.EXTRACTION_FAILED
    assert result.failure_reason is expected_reason
    assert result.character_count is None

    # the file is still stored
    ref = FileRef.for_base_cv(result.base_cv_id, CvContentType.PDF)
    assert await files.get(ref) == cmd.content

    # extracted_text is still None
    stored = await cvs.get(result.base_cv_id)
    assert stored.status is BaseCvStatus.EXTRACTION_FAILED
    assert stored.extracted_text is None
    assert stored.failure_reason is expected_reason

    # BaseCvExtractionFailed was published, with the matching reason
    event_types = [type(event) for event in events.published]
    assert event_types == [BaseCvUploaded, BaseCvExtractionFailed]
    failed_event = events.published[-1]
    assert isinstance(failed_event, BaseCvExtractionFailed)
    assert failed_event.reason is expected_reason


# --- 3. F-14: FileStoreUnavailable propagates, no row is created -----------------------------------


async def test_file_store_unavailable_propagates_and_creates_no_row(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await _active_session(sessions, clock)
    cvs = FakeBaseCvRepository()
    files = AlwaysFailingFileStore()
    extractor = FakeExtractor(outcome=ExtractedText("a" * 200))
    events = RecordingEventPublisher()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock)

    cmd = _command(session.id)

    with pytest.raises(FileStoreUnavailable):
        await use_case(cmd)

    assert cvs.all() == []
    assert events.published == []


# --- 4. Ordering: the file exists before the row ----------------------------------------------


async def test_file_is_written_before_the_row_is_added(clock: FixedClock) -> None:
    """The technical plan's step 5 writes the file **before** step 6/8 create and save the
    aggregate — the deliberate ADR-0006 §2 crash-window choice: a crash here leaves an orphan
    *file* (recoverable by a directory sweep), never an orphan *row* pointing at nothing."""
    sessions = FakeGuestSessionRepository()
    session = await _active_session(sessions, clock)
    cvs = FakeBaseCvRepository()
    files = InMemoryFileStore(repo=cvs)
    extractor = FakeExtractor(outcome=ExtractedText("a" * 200))
    events = RecordingEventPublisher()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock)

    cmd = _command(session.id)
    await use_case(cmd)

    # at the moment `files.put` ran, the repository held nothing yet
    assert files.repo_size_at_put == 0
    # by the time the use case returned, the row exists
    assert len(cvs.all()) == 1


# --- 5. F-23: sixth base CV for one session -----------------------------------------------------


async def test_sixth_base_cv_for_one_session_raises_too_many_base_cvs(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await _active_session(sessions, clock)
    cvs = FakeBaseCvRepository()

    for _ in range(5):
        existing_id = cvs.next_identity()
        existing = BaseCv.upload(
            id=existing_id,
            guest_session_id=session.id,
            original_filename=OriginalFilename("old.pdf"),
            content_type=CvContentType.PDF,
            size_bytes=10,
            file=FileRef.for_base_cv(existing_id, CvContentType.PDF),
            uploaded_at=clock.now(),
        )
        await cvs.add(existing)

    files = InMemoryFileStore()
    extractor = FakeExtractor(outcome=ExtractedText("a" * 200))
    events = RecordingEventPublisher()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock, max_per_session=5)

    cmd = _command(session.id)

    with pytest.raises(TooManyBaseCvs):
        await use_case(cmd)

    # the cap is checked before the file write (step 3, before step 5) — no sixth row, no file
    assert len(cvs.all()) == 5
    assert files.data == {}
    assert events.published == []


# --- 6. Expired / missing guest session -----------------------------------------------------------


async def test_expired_guest_session_raises_guest_session_expired(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    expired = GuestSession.start(
        id=sessions.next_identity(),
        token_hash="b" * 64,
        at=clock.now() - timedelta(hours=48),
        ttl_hours=24,
    )
    await sessions.add(expired)
    cvs = FakeBaseCvRepository()
    files = InMemoryFileStore()
    extractor = FakeExtractor(outcome=ExtractedText("a" * 200))
    events = RecordingEventPublisher()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock)

    cmd = _command(expired.id)

    with pytest.raises(GuestSessionExpired):
        await use_case(cmd)

    assert cvs.all() == []
    assert files.data == {}
    assert events.published == []


async def test_missing_guest_session_raises_guest_session_not_found(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()  # empty: no session was ever added
    cvs = FakeBaseCvRepository()
    files = InMemoryFileStore()
    extractor = FakeExtractor(outcome=ExtractedText("a" * 200))
    events = RecordingEventPublisher()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock)

    unknown_session_id = GuestSessionId(value=uuid4())
    cmd = _command(unknown_session_id)

    with pytest.raises(GuestSessionNotFound):
        await use_case(cmd)

    assert cvs.all() == []
    assert files.data == {}
    assert events.published == []
