"""Application tests for `ListSavedBaseCvs` (T9/T13b, RED, slice 2.2, AC-8, AC-52).

The user-side sibling of `ListBaseCvsForSession` (1.1): "every saved base CV this user owns, newest
first, or an empty sequence — never a 404." Authorized **by construction** — `list_for_user` queries
by exactly the resolved user's id — so this file's job is to prove the use case resolves the user
first (AC-8's "not `UserNotFound`" defense-in-depth every user use case shares) and never returns a
row it does not own, rather than to re-prove `FakeBaseCvRepository.list_for_user`'s own filter (that
belongs to `test_base_cv_repository.py`, once T13b's adapter exists to test).

**Amended for T13b** (technical plan §3, amendment 2026-09-25): `list_for_user` returns
`SavedBaseCvSummary`, not `BaseCv` — a read model with `character_count` computed instead of the text
itself, so AC-52 ("the list never selects `extracted_text`") and AC-1 ("the list reports a character
count") can both hold. These tests assert the summary's fields mirror the source aggregate one for
one, that a non-extracted or failed CV reports no count, and that the summary type itself carries no
attribute a CV's text could ever be smuggled through.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tailorcraft.application.intake.list_saved_base_cvs import ListSavedBaseCvs
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    GuestSessionId,
    PasswordHash,
    UserId,
)
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.saved_base_cv_summary import SavedBaseCvSummary
from tailorcraft.domain.intake.value_objects import (
    BaseCvLabel,
    BaseCvStatus,
    CvContentType,
    ExtractedText,
    ExtractionFailureReason,
    OriginalFilename,
)
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import FakeBaseCvRepository, FakeUserRepository

_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZWhhc2g")

# 60 repetitions of a 4-code-point, 5-byte-in-UTF-8 word: 299 code points, 359 UTF-8 bytes, 240
# non-whitespace characters — clears ExtractedText's 200-character floor and makes code points and
# bytes diverge, so a `character_count` computed from the wrong unit would be caught here.
_NON_ASCII_TEXT = " ".join(["café"] * 60)


async def _seed_user(users: FakeUserRepository, *, email: str = "alex@example.com") -> User:
    user = User.register_with_password(
        users.next_identity(),
        EmailAddress.parse(email),
        _HASH,
        at=datetime(2026, 9, 4, 0, 0, 0, tzinfo=UTC),
    )
    user.release_events()
    await users.add(user)
    return user


def _saved_cv(cvs: FakeBaseCvRepository, owner: UserOwner, *, uploaded_at: datetime) -> BaseCv:
    cv_id = cvs.next_identity()
    return BaseCv.upload(
        id=cv_id,
        owner=owner,
        original_filename=OriginalFilename("cv.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=10,
        file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
        uploaded_at=uploaded_at,
    )


async def test_a_user_with_no_saved_cvs_gets_an_empty_sequence_not_a_404() -> None:
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()
    use_case = ListSavedBaseCvs(cvs, users)

    result = await use_case(user.id)

    assert list(result) == []


async def test_a_gone_user_raises_user_not_found() -> None:
    users = FakeUserRepository()  # empty: no such user
    cvs = FakeBaseCvRepository()
    use_case = ListSavedBaseCvs(cvs, users)

    with pytest.raises(UserNotFound):
        await use_case(UserId(value=uuid4()))


async def test_returns_only_this_users_saved_cvs_newest_first(clock: FixedClock) -> None:
    """Another user's saved CV and a guest's workspace CV both exist; neither is in the answer, and
    this user's own three come back newest-uploaded-first, as `SavedBaseCvSummary`s."""
    users = FakeUserRepository()
    user = await _seed_user(users, email="alex@example.com")
    other_user = await _seed_user(users, email="other@example.com")
    cvs = FakeBaseCvRepository()

    oldest = _saved_cv(cvs, UserOwner(user.id), uploaded_at=clock.now())
    middle = _saved_cv(cvs, UserOwner(user.id), uploaded_at=clock.now().replace(hour=13))
    newest = _saved_cv(cvs, UserOwner(user.id), uploaded_at=clock.now().replace(hour=14))
    await cvs.add(oldest)
    await cvs.add(middle)
    await cvs.add(newest)

    # a decoy from another user and a decoy from a guest session — neither must appear
    await cvs.add(_saved_cv(cvs, UserOwner(other_user.id), uploaded_at=clock.now()))
    guest_decoy_id = cvs.next_identity()
    guest_decoy = BaseCv.upload(
        id=guest_decoy_id,
        owner=GuestOwner(GuestSessionId(value=uuid4())),
        original_filename=OriginalFilename("guest.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=10,
        file=FileRef.for_base_cv(guest_decoy_id, CvContentType.PDF),
        uploaded_at=clock.now(),
    )
    await cvs.add(guest_decoy)

    use_case = ListSavedBaseCvs(cvs, users)
    result = await use_case(user.id)

    assert all(isinstance(item, SavedBaseCvSummary) for item in result)
    assert [item.id for item in result] == [newest.id, middle.id, oldest.id]
    assert guest_decoy.id not in [item.id for item in result]


async def test_summary_mirrors_an_extracted_cvs_fields_field_by_field(clock: FixedClock) -> None:
    """Every field the summary carries for an `EXTRACTED` CV equals the source aggregate's own —
    including `character_count`, computed from a non-ASCII text whose code-point length differs from
    its UTF-8 byte length, so a count computed in the wrong unit would disagree with this assertion."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()

    cv = _saved_cv(cvs, UserOwner(user.id), uploaded_at=clock.now())
    cv.rename(BaseCvLabel("Backend roles"), at=clock.now())
    text = ExtractedText(value=_NON_ASCII_TEXT)
    cv.mark_extracted(text, at=clock.now())
    await cvs.add(cv)

    use_case = ListSavedBaseCvs(cvs, users)
    result = await use_case(user.id)

    assert len(result) == 1
    summary = result[0]
    assert isinstance(summary, SavedBaseCvSummary)
    assert summary.id == cv.id
    assert summary.label == cv.label
    assert summary.original_filename == cv.original_filename
    assert summary.content_type == cv.content_type
    assert summary.size_bytes == cv.size_bytes
    assert summary.status == BaseCvStatus.EXTRACTED
    assert summary.failure_reason is None
    assert summary.uploaded_at == cv.uploaded_at
    # the whole point: a code-point count, not a byte count, and not the text itself.
    assert text.character_count == len(_NON_ASCII_TEXT)
    assert len(_NON_ASCII_TEXT) != len(_NON_ASCII_TEXT.encode("utf-8"))
    assert summary.character_count == len(_NON_ASCII_TEXT)


async def test_summary_for_a_failed_extraction_has_no_character_count(clock: FixedClock) -> None:
    """A CV whose extraction failed reports `character_count: None` — there is no text to count —
    and names the reason, mirroring the aggregate's own `EXTRACTION_FAILED` state (I-2)."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()

    cv = _saved_cv(cvs, UserOwner(user.id), uploaded_at=clock.now())
    cv.mark_extraction_failed(ExtractionFailureReason.CORRUPT, at=clock.now())
    await cvs.add(cv)

    use_case = ListSavedBaseCvs(cvs, users)
    result = await use_case(user.id)

    assert len(result) == 1
    summary = result[0]
    assert summary.status == BaseCvStatus.EXTRACTION_FAILED
    assert summary.character_count is None
    assert summary.failure_reason == ExtractionFailureReason.CORRUPT


async def test_summary_for_a_not_yet_decided_cv_has_no_character_count(clock: FixedClock) -> None:
    """A freshly-uploaded CV (`status: UPLOADED`, extraction not yet decided) also reports
    `character_count: None` — the same "no text, no count" rule as a failed extraction, for the
    other reason there is no text yet."""
    users = FakeUserRepository()
    user = await _seed_user(users)
    cvs = FakeBaseCvRepository()

    cv = _saved_cv(cvs, UserOwner(user.id), uploaded_at=clock.now())
    await cvs.add(cv)

    use_case = ListSavedBaseCvs(cvs, users)
    result = await use_case(user.id)

    assert len(result) == 1
    summary = result[0]
    assert summary.status == BaseCvStatus.UPLOADED
    assert summary.character_count is None
    assert summary.failure_reason is None


def test_the_summary_type_has_no_attribute_that_could_hold_the_extracted_text() -> None:
    """AC-52 at the type level, not just at the query level: `SavedBaseCvSummary` is a frozen,
    slotted dataclass, so its complete attribute surface is exactly its `__slots__` — asserting that
    set proves no field named `extracted_text`, `text` or `value` (an `ExtractedText`-shaped escape
    hatch) can exist on an instance, now or after an unnoticed edit to the dataclass, rather than
    merely checking that one instance's fields happen not to be named that today."""
    assert set(SavedBaseCvSummary.__slots__) == {
        "id",
        "label",
        "original_filename",
        "content_type",
        "size_bytes",
        "status",
        "character_count",
        "failure_reason",
        "uploaded_at",
    }
    assert {f.name for f in dataclasses.fields(SavedBaseCvSummary)} == set(
        SavedBaseCvSummary.__slots__
    )
