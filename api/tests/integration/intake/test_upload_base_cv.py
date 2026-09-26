"""Application tests for `UploadBaseCv` (T9, RED).

**Why these fakes, not a real Postgres:** the technical plan's Test plan says this suite runs
"against the real test DB with transactional rollback", and it will — once T14-T17 land the
imperative mappings, the repositories and the first Alembic migration. As of this commit none of
that exists (`infrastructure/persistence/mapping/` has no mapping modules, and there is no
migration to bring `tailorcraft_test` to head), so a test that imported `conftest.py`'s `session` /
`engine` fixtures would fail for a reason that has nothing to do with `UploadBaseCv`. Writing the
red honestly means testing the use case against the ports it actually depends on: in-memory fakes
of `BaseCvRepository`, `GuestSessionRepository` and `FileStorePort`, imported from
`tests/integration/fakes.py` (shared with T12's read-side and `StartGuestSession` tests — see that
module's docstring for why they live there rather than being copied per test module) — each
satisfying its Protocol exactly. **T28** is where the real persistence round-trip gets its own
test, once the repositories exist to round-trip through.

The aggregates are not faked — `BaseCv` and `GuestSession` are the real domain classes.

Every assertion below states what `UploadBaseCv.__call__` should do per technical-plan.md's
"Flow" section and feature-spec.md's failure contract, never what the (currently `NotImplementedError`)
code was observed doing.

**Section 7 (T9, slice 2.2)** adds the `UserOwner` arm (AC-7, S-2, S-4): the same use case storing a
signed-in user's saved CV rather than a guest's workspace one. The guest-arm tests above are
untouched in meaning — 1.1's command field was renamed mechanically (T8) and every one of them
already passes against the skeleton's unchanged guest arm; only section 7 is new, and only it is
red against the `UserOwner` arm's `NotImplementedError` (`upload_base_cv.py`'s own `match`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.intake.upload_base_cv import (
    UploadBaseCv,
    UploadBaseCvCommand,
    UploadBaseCvResult,
)
from tailorcraft.domain.identity.errors import (
    GuestSessionExpired,
    GuestSessionNotFound,
    UserNotFound,
)
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    GuestSessionId,
    PasswordHash,
    UserId,
)
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import (
    CorruptCvFile,
    CvExtractionFailed,
    CvHasNoTextLayer,
    CvHasTooManyPages,
    CvTextTooShort,
    EncryptedCvFile,
    TooManyBaseCvs,
    TooManySavedBaseCvs,
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
    BaseCvStatus,
    CvContentType,
    ExtractedText,
    ExtractionFailureReason,
    OriginalFilename,
)
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    AlwaysFailingFileStore,
    FakeBaseCvRepository,
    FakeExtractor,
    FakeGuestSessionRepository,
    FakeUserRepository,
    InMemoryFileStore,
    RecordingEventPublisher,
    create_active_session,
)

# --- Test helpers --------------------------------------------------------------------------------


def _command(
    session_id: GuestSessionId,
    *,
    filename: str = "cv.pdf",
    content_type: CvContentType = CvContentType.PDF,
    content: bytes = b"content bytes for a fake upload, not a real PDF",
) -> UploadBaseCvCommand:
    return UploadBaseCvCommand(
        owner=GuestOwner(session_id),
        original_filename=OriginalFilename(filename),
        content_type=content_type,
        content=content,
    )


# --- 1. Happy path -------------------------------------------------------------------------------


async def test_happy_path_stores_extracts_and_publishes(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    cvs = FakeBaseCvRepository()
    files = InMemoryFileStore()
    text = ExtractedText("word " * 200)
    extractor = FakeExtractor(outcome=text)
    events = RecordingEventPublisher()
    users = FakeUserRepository()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock, users)

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
    session = await create_active_session(sessions, clock)
    cvs = FakeBaseCvRepository()
    files = InMemoryFileStore()
    extractor = FakeExtractor(outcome=exc)
    events = RecordingEventPublisher()
    users = FakeUserRepository()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock, users)

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
    session = await create_active_session(sessions, clock)
    cvs = FakeBaseCvRepository()
    files = AlwaysFailingFileStore()
    extractor = FakeExtractor(outcome=ExtractedText("a" * 200))
    events = RecordingEventPublisher()
    users = FakeUserRepository()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock, users)

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
    session = await create_active_session(sessions, clock)
    cvs = FakeBaseCvRepository()
    files = InMemoryFileStore(repo=cvs)
    extractor = FakeExtractor(outcome=ExtractedText("a" * 200))
    events = RecordingEventPublisher()
    users = FakeUserRepository()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock, users)

    cmd = _command(session.id)
    await use_case(cmd)

    # at the moment `files.put` ran, the repository held nothing yet
    assert files.repo_size_at_put == 0
    # by the time the use case returned, the row exists
    assert len(cvs.all()) == 1


# --- 5. F-23: sixth base CV for one session -----------------------------------------------------


async def test_sixth_base_cv_for_one_session_raises_too_many_base_cvs(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    cvs = FakeBaseCvRepository()

    for _ in range(5):
        existing_id = cvs.next_identity()
        existing = BaseCv.upload(
            id=existing_id,
            owner=GuestOwner(session.id),
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
    users = FakeUserRepository()
    use_case = UploadBaseCv(
        cvs, sessions, files, extractor, events, clock, users, max_per_session=5
    )

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
    users = FakeUserRepository()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock, users)

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
    users = FakeUserRepository()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock, users)

    unknown_session_id = GuestSessionId(value=uuid4())
    cmd = _command(unknown_session_id)

    with pytest.raises(GuestSessionNotFound):
        await use_case(cmd)

    assert cvs.all() == []
    assert files.data == {}
    assert events.published == []


# --- 7. Slice 2.2 — the `UserOwner` arm (AC-7, S-2, S-4) ------------------------------------------


def _user_command(
    user_id: UserId,
    *,
    filename: str = "cv.pdf",
    content_type: CvContentType = CvContentType.PDF,
    content: bytes = b"content bytes for a fake upload, not a real PDF",
) -> UploadBaseCvCommand:
    return UploadBaseCvCommand(
        owner=UserOwner(user_id),
        original_filename=OriginalFilename(filename),
        content_type=content_type,
        content=content,
    )


async def _seed_user(users: FakeUserRepository, *, email: str = "alex@example.com") -> User:
    """A user "already in the database" — mirrors `test_log_in.py`'s helper of the same name:
    built directly through `User.register_with_password`, its creation event discarded, since this
    file tests `UploadBaseCv`, not registration."""
    user = User.register_with_password(
        users.next_identity(),
        EmailAddress.parse(email),
        PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZWhhc2g"),
        at=datetime(2026, 9, 4, 0, 0, 0, tzinfo=UTC),
    )
    user.release_events()
    await users.add(user)
    return user


async def test_uploading_for_a_user_owner_stores_the_cv_owned_by_the_user(
    clock: FixedClock,
) -> None:
    """AC-7's `UserOwner` arm: `users.get` resolves the owner, and the stored `BaseCv` ends up
    `UserOwner`-owned — never `GuestOwner`-owned, and never the guest arm's session lookup."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    sessions = FakeGuestSessionRepository()  # never touched by this arm
    cvs = FakeBaseCvRepository()
    files = InMemoryFileStore()
    text = ExtractedText("word " * 200)
    extractor = FakeExtractor(outcome=text)
    events = RecordingEventPublisher()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock, users)

    cmd = _user_command(user.id)
    result = await use_case(cmd)

    assert result.status is BaseCvStatus.EXTRACTED
    stored = await cvs.get(result.base_cv_id)
    assert stored.owner == UserOwner(user.id)

    event_types = [type(event) for event in events.published]
    assert event_types == [BaseCvUploaded, BaseCvTextExtracted]


async def test_uploading_for_a_gone_user_raises_user_not_found_and_stores_nothing(
    clock: FixedClock,
) -> None:
    """S-2: a valid bearer whose user row is gone (erased within the access token's 15 minutes).
    Checked before the file write, exactly as the guest arm's cap and expiry checks are."""
    users = FakeUserRepository()  # empty: no such user
    sessions = FakeGuestSessionRepository()
    cvs = FakeBaseCvRepository()
    files = InMemoryFileStore()
    extractor = FakeExtractor(outcome=ExtractedText("a" * 200))
    events = RecordingEventPublisher()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock, users)

    gone_user_id = UserId(value=uuid4())
    cmd = _user_command(gone_user_id)

    with pytest.raises(UserNotFound):
        await use_case(cmd)

    assert cvs.all() == []
    assert files.data == {}
    assert events.published == []


async def test_sixth_saved_base_cv_for_one_user_raises_too_many_saved_base_cvs(
    clock: FixedClock,
) -> None:
    """S-4 / AC-7's cap: `TooManySavedBaseCvs`, `max_saved_base_cvs_per_user`'s sibling of the
    guest arm's `TooManyBaseCvs` — checked before the file write, so a refused 6th CV writes
    nothing."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    sessions = FakeGuestSessionRepository()
    cvs = FakeBaseCvRepository()

    for _ in range(5):
        existing_id = cvs.next_identity()
        existing = BaseCv.upload(
            id=existing_id,
            owner=UserOwner(user.id),
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
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock, users, max_per_user=5)

    cmd = _user_command(user.id)

    with pytest.raises(TooManySavedBaseCvs):
        await use_case(cmd)

    assert len(cvs.all()) == 5
    assert files.data == {}
    assert events.published == []


async def test_user_owned_file_is_written_before_the_row_is_added(clock: FixedClock) -> None:
    """AC-7: "the file is written **before** the row in both arms" — the guest arm's ordering test
    (section 4 above), repeated for the `UserOwner` arm rather than assumed to carry over."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    sessions = FakeGuestSessionRepository()
    cvs = FakeBaseCvRepository()
    files = InMemoryFileStore(repo=cvs)
    extractor = FakeExtractor(outcome=ExtractedText("a" * 200))
    events = RecordingEventPublisher()
    use_case = UploadBaseCv(cvs, sessions, files, extractor, events, clock, users)

    cmd = _user_command(user.id)
    await use_case(cmd)

    assert files.repo_size_at_put == 0
    assert len(cvs.all()) == 1
