"""Application tests for `CopySavedBaseCvToWorkspace` (T9, RED, slice 2.2, AC-9, S-27, S-28, S-30,
S-32, S-33).

**No extractor anywhere in this file** — the constructor does not take one (AC-9's strongest form,
the skeleton's own docstring), so there is nothing to configure and nothing to assert a call count
against. `InMemoryFileStore.get`'s `StoredFileMissing` on a missing key (T9's fakes addition, see
`fakes.py`) is what drives S-32/S-33: neither test needs a bespoke fake, since "the file was never
`put`" already reproduces the port's documented failure honestly.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.intake.copy_saved_base_cv import CopySavedBaseCvToWorkspace
from tailorcraft.application.intake.upload_base_cv import UploadBaseCvResult
from tailorcraft.domain.identity.errors import GuestSessionExpired, UserNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    PasswordHash,
    UserId,
)
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import (
    BaseCvNotFound,
    BaseCvNotOwnedByUser,
    SavedBaseCvFileMissing,
    SavedBaseCvNotCopyable,
    TooManyBaseCvs,
)
from tailorcraft.domain.intake.events import BaseCvCopied
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    BaseCvStatus,
    CvContentType,
    ExtractedText,
    ExtractionFailureReason,
    OriginalFilename,
)
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeBaseCvRepository,
    FakeGuestSessionRepository,
    FakeUserRepository,
    InMemoryFileStore,
    RecordingEventPublisher,
    create_active_session,
)

_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZWhhc2g")
_SOURCE_TEXT = ExtractedText("word " * 200)


class _DeletingBaseCvRepository(FakeBaseCvRepository):
    """Deletes the row on **every** `get` call — models S-33: the source is deleted between this
    use case's first load and its `StoredFileMissing` re-read, so the re-read finds nothing."""

    async def get(self, cv_id: BaseCvId) -> BaseCv:
        cv = await super().get(cv_id)
        del self._by_id[cv_id]
        return cv


async def _seed_user(users: FakeUserRepository, *, email: str = "alex@example.com") -> User:
    user = User.register_with_password(
        users.next_identity(), EmailAddress.parse(email), _HASH, at=datetime(2026, 9, 4, tzinfo=UTC)
    )
    user.release_events()
    await users.add(user)
    return user


def _extracted_source(cvs: FakeBaseCvRepository, owner: UserOwner, at: datetime) -> BaseCv:
    cv_id = cvs.next_identity()
    cv = BaseCv.upload(
        id=cv_id,
        owner=owner,
        original_filename=OriginalFilename("cv.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=10,
        file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
        uploaded_at=at,
    )
    cv.mark_extracted(_SOURCE_TEXT, at)
    cv.release_events()
    return cv


def _build_use_case(
    cvs: FakeBaseCvRepository,
    users: FakeUserRepository,
    sessions: FakeGuestSessionRepository,
    files: InMemoryFileStore,
    events: RecordingEventPublisher,
    clock: FixedClock,
    *,
    max_per_session: int = 5,
) -> CopySavedBaseCvToWorkspace:
    return CopySavedBaseCvToWorkspace(
        cvs, users, sessions, files, events, clock, max_per_session=max_per_session
    )


async def test_happy_path_copies_the_bytes_and_the_extracted_text_and_publishes_after_add(
    clock: FixedClock,
) -> None:
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()
    source = _extracted_source(cvs, UserOwner(user.id), clock.now())
    await cvs.add(source)
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    files = InMemoryFileStore()
    await files.put(source.file, b"the source pdf's bytes")
    events = RecordingEventPublisher(repo=cvs)
    use_case = _build_use_case(cvs, users, sessions, files, events, clock)

    result = await use_case(source.id, user.id, session.id)

    assert isinstance(result, UploadBaseCvResult)
    assert result.status is BaseCvStatus.EXTRACTED
    assert result.character_count == _SOURCE_TEXT.character_count
    assert result.base_cv_id != source.id

    copy = await cvs.get(result.base_cv_id)
    assert copy.owner == GuestOwner(session.id)
    assert copy.copied_from == source.id
    assert copy.file != source.file
    assert await files.get(copy.file) == b"the source pdf's bytes"
    # the source is untouched
    still_source = await cvs.get(source.id)
    assert still_source.owner == UserOwner(user.id)
    assert still_source.copied_from is None

    # BaseCvCopied only after the copy's row was added
    event_types = [type(event) for event in events.published]
    assert event_types == [BaseCvCopied]
    assert events.repo_size_at_first_publish == 2  # source + the newly-added copy


async def test_a_gone_user_raises_user_not_found(clock: FixedClock) -> None:
    users = FakeUserRepository()  # empty
    cvs = FakeBaseCvRepository()
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = _build_use_case(cvs, users, sessions, files, events, clock)

    with pytest.raises(UserNotFound):
        await use_case(BaseCvId(value=uuid4()), UserId(value=uuid4()), session.id)

    assert files.data == {}
    assert events.published == []


async def test_a_nonexistent_source_id_raises_base_cv_not_found(clock: FixedClock) -> None:
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()  # empty
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = _build_use_case(cvs, users, sessions, files, events, clock)

    with pytest.raises(BaseCvNotFound):
        await use_case(BaseCvId(value=uuid4()), user.id, session.id)


async def test_a_source_owned_by_another_user_raises_base_cv_not_found_from_not_owned_by_user(
    clock: FixedClock,
) -> None:
    """S-27: "source not the bearer's" — the same 404 as a nonexistent id, distinguishable only on
    `__cause__`."""
    users = FakeUserRepository()
    owner = await _seed_user(users, email="owner@example.com")
    attacker = await _seed_user(users, email="attacker@example.com")
    cvs = FakeBaseCvRepository()
    source = _extracted_source(cvs, UserOwner(owner.id), clock.now())
    await cvs.add(source)
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    files = InMemoryFileStore()
    await files.put(source.file, b"bytes")
    events = RecordingEventPublisher()
    use_case = _build_use_case(cvs, users, sessions, files, events, clock)

    with pytest.raises(BaseCvNotFound) as exc_info:
        await use_case(source.id, attacker.id, session.id)

    assert isinstance(exc_info.value.__cause__, BaseCvNotOwnedByUser)
    assert events.published == []


def _leave_uploaded(cv: BaseCv, at: datetime) -> None:
    pass


def _mark_extraction_failed(cv: BaseCv, at: datetime) -> None:
    cv.mark_extraction_failed(ExtractionFailureReason.CORRUPT, at)


@pytest.mark.parametrize(
    "make_source_status",
    [
        pytest.param(_leave_uploaded, id="uploaded-not-yet-extracted"),
        pytest.param(_mark_extraction_failed, id="extraction_failed"),
    ],
)
async def test_a_source_not_yet_extracted_raises_saved_base_cv_not_copyable(
    clock: FixedClock, make_source_status: Callable[[BaseCv, datetime], None]
) -> None:
    """S-28: a working copy is only ever `EXTRACTED` (the copy never re-runs the extractor), so a
    source that is `uploaded` or `extraction_failed` cannot be copied."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()
    cv_id = cvs.next_identity()
    source = BaseCv.upload(
        id=cv_id,
        owner=UserOwner(user.id),
        original_filename=OriginalFilename("cv.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=10,
        file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
        uploaded_at=clock.now(),
    )
    make_source_status(source, clock.now())
    source.release_events()
    await cvs.add(source)
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    files = InMemoryFileStore()
    events = RecordingEventPublisher()
    use_case = _build_use_case(cvs, users, sessions, files, events, clock)

    with pytest.raises(SavedBaseCvNotCopyable) as exc_info:
        await use_case(source.id, user.id, session.id)

    assert exc_info.value.status is source.status
    assert files.data == {}
    assert events.published == []


async def test_the_guest_session_expired_raises_guest_session_expired(clock: FixedClock) -> None:
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()
    source = _extracted_source(cvs, UserOwner(user.id), clock.now())
    await cvs.add(source)
    sessions = FakeGuestSessionRepository()
    expired = GuestSession.start(
        id=sessions.next_identity(),
        token_hash="e" * 64,
        at=clock.now() - timedelta(hours=48),
        ttl_hours=24,
    )
    await sessions.add(expired)
    files = InMemoryFileStore()
    await files.put(source.file, b"bytes")
    events = RecordingEventPublisher()
    use_case = _build_use_case(cvs, users, sessions, files, events, clock)

    with pytest.raises(GuestSessionExpired):
        await use_case(source.id, user.id, expired.id)

    assert files.data == {}
    assert events.published == []


async def test_the_guest_cap_reached_raises_too_many_base_cvs(clock: FixedClock) -> None:
    """S-30: the **guest** cap applies to the destination — `TooManyBaseCvs`, not
    `TooManySavedBaseCvs`."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()
    source = _extracted_source(cvs, UserOwner(user.id), clock.now())
    await cvs.add(source)
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
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
    await files.put(source.file, b"bytes")
    events = RecordingEventPublisher()
    use_case = _build_use_case(cvs, users, sessions, files, events, clock, max_per_session=5)

    with pytest.raises(TooManyBaseCvs):
        await use_case(source.id, user.id, session.id)

    # still 6 rows total (source + 5 guest CVs) — no copy was added
    assert len(cvs.all()) == 6
    assert events.published == []


async def test_a_missing_source_file_with_the_row_still_present_raises_saved_base_cv_file_missing(
    clock: FixedClock,
) -> None:
    """S-32: the row exists but the bytes do not — a bug somewhere, worth its own answer rather
    than a 404 that says the row never existed. The re-read finds the row still there."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()
    source = _extracted_source(cvs, UserOwner(user.id), clock.now())
    await cvs.add(source)
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    files = InMemoryFileStore()  # the source's bytes were never `put` — StoredFileMissing on `get`
    events = RecordingEventPublisher()
    use_case = _build_use_case(cvs, users, sessions, files, events, clock)

    with pytest.raises(SavedBaseCvFileMissing):
        await use_case(source.id, user.id, session.id)

    assert events.published == []
    # the row is still there — this is what distinguishes S-32 from S-33
    assert (await cvs.get(source.id)) is source


async def test_a_source_deleted_between_load_and_file_read_raises_base_cv_not_found(
    clock: FixedClock,
) -> None:
    """S-33: the row itself disappears between this use case's first `get` and its
    `StoredFileMissing` re-read — a concurrent delete, not a bug: `BaseCvNotFound`, not
    `SavedBaseCvFileMissing`."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = _DeletingBaseCvRepository()
    source = _extracted_source(cvs, UserOwner(user.id), clock.now())
    await cvs.add(source)
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    files = InMemoryFileStore()  # never `put` — the first `get` raises `StoredFileMissing`
    events = RecordingEventPublisher()
    use_case = _build_use_case(cvs, users, sessions, files, events, clock)

    with pytest.raises(BaseCvNotFound):
        await use_case(source.id, user.id, session.id)

    assert events.published == []
