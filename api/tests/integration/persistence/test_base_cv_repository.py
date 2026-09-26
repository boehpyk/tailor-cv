"""Persistence tests for `BaseCv` — the imperative mapping, its `TypeDecorator`s and
`SqlAlchemyBaseCvRepository` against real PostgreSQL (T28, written **after**).

Every assertion states what technical-plan.md's persistence section and `domain/intake/base_cv.py`'s
invariants (I-1…I-5) say should happen, not what a first run of the code produced.

As in the `GuestSession` module, `session.expunge_all()` forces a genuine reload before every
round-trip assertion — otherwise the ORM's identity map would hand back the exact object `add()`
was given, proving nothing about the mapping or the `TypeDecorator`s.

**Slice 2.2 additions (T15, after).** Everything from "`UserOwner` round trip" onward proves the
owner sum type's `UserOwner` half and the four repository methods 2.2 adds
(`list_for_user`/`count_for_user`/`save_label`/`remove`), against the real `intake_base_cv` schema
`1a2676aa3759` migrated in. `_persist_user` is this file's analogue of `_persist_owner`, for a saved
CV's other possible owner.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tailorcraft.domain.identity.errors import UserNotFound
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
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.intake.saved_base_cv_summary import SavedBaseCvSummary
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    BaseCvLabel,
    BaseCvStatus,
    CvContentType,
    ExtractedText,
    ExtractionFailureReason,
    OriginalFilename,
)
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)

_PASSWORD_HASH = PasswordHash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA")

# 60 repetitions of a 4-code-point, 5-byte-in-UTF-8 word: 299 code points, 359 UTF-8 bytes — clears
# `ExtractedText`'s 200-character floor and makes code points and bytes diverge, exactly
# `test_list_saved_base_cvs.py`'s fixture, so a `character_count` computed in the wrong unit is
# caught by both the fake-backed use-case test and this real one.
_NON_ASCII_TEXT = " ".join(["café"] * 60)

# --- Test helpers --------------------------------------------------------------------------------


async def _persist_owner(
    session: AsyncSession, clock: FixedClock, *, token_hash: str
) -> GuestSession:
    """A `BaseCv` needs a real, persisted `GuestSession` to satisfy the `NOT NULL` FK
    (`intake_base_cv.guest_session_id`) — every test below builds one first."""
    repo = SqlAlchemyGuestSessionRepository(session)
    owner = GuestSession.start(
        id=repo.next_identity(), token_hash=token_hash, at=clock.now(), ttl_hours=24
    )
    await repo.add(owner)
    return owner


def _upload(
    cvs: SqlAlchemyBaseCvRepository,
    owner_id: GuestSessionId,
    clock: FixedClock,
    *,
    content_type: CvContentType = CvContentType.PDF,
    filename: str = "cv.pdf",
    size_bytes: int = 1234,
) -> BaseCv:
    cv_id = cvs.next_identity()
    return BaseCv.upload(
        id=cv_id,
        owner=GuestOwner(owner_id),
        original_filename=OriginalFilename(filename),
        content_type=content_type,
        size_bytes=size_bytes,
        file=FileRef.for_base_cv(cv_id, content_type),
        uploaded_at=clock.now(),
    )


async def _persist_user(session: AsyncSession, clock: FixedClock, *, email: str) -> UserId:
    """A saved (`UserOwner`) `BaseCv` needs a real, persisted `User` to satisfy
    `fk_intake_base_cv_user_id_identity_user` — every 2.2 test below builds one first, exactly as
    `test_login_repository.py`'s `_persist_user` does for `identity_login`."""
    users = SqlAlchemyUserRepository(session)
    user_id = users.next_identity()
    await users.add(
        User.register_with_password(
            id=user_id,
            email=EmailAddress.parse(email),
            password_hash=_PASSWORD_HASH,
            at=clock.now(),
        )
    )
    return user_id


def _saved_upload(
    cvs: SqlAlchemyBaseCvRepository,
    owner_id: UserId,
    clock: FixedClock,
    *,
    filename: str = "cv.pdf",
    uploaded_at: datetime | None = None,
) -> BaseCv:
    cv_id = cvs.next_identity()
    return BaseCv.upload(
        id=cv_id,
        owner=UserOwner(owner_id),
        original_filename=OriginalFilename(filename),
        content_type=CvContentType.PDF,
        size_bytes=10,
        file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
        uploaded_at=uploaded_at if uploaded_at is not None else clock.now(),
    )


# --- Mapping round-trip: every value object comes back as its own type ---------------------------


async def test_round_trip_of_an_extracted_cv_preserves_value_object_types(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="1" * 64)
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _upload(cvs, owner.id, clock, size_bytes=1234)
    text = ExtractedText("word " * 200)
    cv.mark_extracted(text, clock.now())
    await cvs.add(cv)

    session.expunge_all()
    reloaded = await cvs.get(cv.id)

    assert isinstance(reloaded.id, BaseCvId)
    assert reloaded.id == cv.id
    assert isinstance(reloaded.owner, GuestOwner)
    assert reloaded.owner == GuestOwner(owner.id)
    assert isinstance(reloaded.original_filename, OriginalFilename)
    assert reloaded.original_filename == cv.original_filename
    assert isinstance(reloaded.content_type, CvContentType)
    assert reloaded.content_type is CvContentType.PDF
    assert reloaded.size_bytes == 1234
    assert isinstance(reloaded.file, FileRef)
    assert reloaded.file == cv.file
    assert isinstance(reloaded.status, BaseCvStatus)
    assert reloaded.status is BaseCvStatus.EXTRACTED
    assert isinstance(reloaded.extracted_text, ExtractedText)
    assert reloaded.extracted_text == text
    assert reloaded.failure_reason is None


async def test_round_trip_of_a_failed_extraction_preserves_the_failure_reason_type(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="2" * 64)
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _upload(cvs, owner.id, clock)
    cv.mark_extraction_failed(ExtractionFailureReason.ENCRYPTED, clock.now())
    await cvs.add(cv)

    session.expunge_all()
    reloaded = await cvs.get(cv.id)

    assert reloaded.status is BaseCvStatus.EXTRACTION_FAILED
    assert reloaded.extracted_text is None
    assert isinstance(reloaded.failure_reason, ExtractionFailureReason)
    assert reloaded.failure_reason is ExtractionFailureReason.ENCRYPTED


# --- `TypeDecorator` round-trips including NULL (invariant I-2, both directions) -----------------


async def test_round_trip_of_a_freshly_uploaded_cv_leaves_both_extraction_columns_null(
    session: AsyncSession, clock: FixedClock
) -> None:
    """I-2's third state: `status == UPLOADED` means neither `extracted_text` nor
    `extraction_failure_reason` has been decided yet. Both `ExtractedTextType` and
    `ExtractionFailureReasonType` must round-trip `NULL` cleanly, and `extracted_at` (nullable
    `TIMESTAMP`) along with them."""
    owner = await _persist_owner(session, clock, token_hash="3" * 64)
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _upload(cvs, owner.id, clock, content_type=CvContentType.TXT, filename="cv.txt")
    await cvs.add(cv)

    session.expunge_all()
    reloaded = await cvs.get(cv.id)

    assert reloaded.status is BaseCvStatus.UPLOADED
    assert reloaded.extracted_text is None
    assert reloaded.failure_reason is None
    assert reloaded.extracted_at is None


# --- Whole-second timestamp fidelity ---------------------------------------------------------------


async def test_round_trip_preserves_whole_second_timestamps(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="4" * 64)
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _upload(cvs, owner.id, clock)
    cv.mark_extracted(ExtractedText("word " * 200), clock.now())
    await cvs.add(cv)

    session.expunge_all()
    reloaded = await cvs.get(cv.id)

    assert reloaded.uploaded_at.microsecond == 0
    assert reloaded.extracted_at is not None
    assert reloaded.extracted_at.microsecond == 0


# --- Repository behaviour ---------------------------------------------------------------------------


async def test_get_raises_not_found_for_an_unknown_id(session: AsyncSession) -> None:
    cvs = SqlAlchemyBaseCvRepository(session)

    with pytest.raises(BaseCvNotFound):
        await cvs.get(cvs.next_identity())


async def test_a_second_get_of_the_same_id_is_an_identity_map_hit_not_a_second_decode(
    session: AsyncSession, clock: FixedClock
) -> None:
    """T30b-A / `/verify` round 1 MINOR (`repositories/intake/base_cv.py:101-165`). The copy route's
    own shape is two `get()` calls for the same id inside one request — the router's authorization
    read, then the use case's own internal authorization read, on the same session — and `get`'s
    module docstring says the second must be an identity-map hit, never a second run of
    `ExtractedTextType`'s decode (the whole point of `_pinned`: the session's identity map holds only
    a *weak* reference, so without a strong one the router's own dropped local would let the
    instance be collected between the two calls).

    Counted by wrapping `ExtractedText.__post_init__` — dataclass machinery a fake object cannot
    stand in for, so a fresh call is the one thing that can only mean "a row was just decoded".

    **Mutation, observed red 2026-09-26 and reverted byte-exact.** Replaced the whole method body
    with a plain `select(BaseCv).where(BaseCv._id == cv_id)` — the exact shape the docstring says
    `get` was changed *away from* ("Why not `select(BaseCv)`"). Note that swapping only the
    identity-map branch for an unconditional `session.get(BaseCv, cv_id)` does **not** reproduce the
    bug: `session.get` itself special-cases an identity-map hit and returns the cached instance
    without executing SQL at all, so no `TypeDecorator` runs and this test stays green — the bug is
    specifically that a `select()` statement decodes every column as a side effect of processing the
    row, discarding the freshly-decoded object in favour of the identity map's cached one only
    *after* the decode has already happened. Re-run:
    ```
    >       assert constructions == 1, (
                f"ExtractedText was constructed {constructions} time(s) for two get() calls of the "
                "same id in one repository's lifetime — the second call must be an identity-map hit"
            )
    E       AssertionError: ExtractedText was constructed 2 time(s) for two get() calls of the same
    id in one repository's lifetime — the second call must be an identity-map hit
    E       assert 2 == 1
    FAILED tests/integration/persistence/test_base_cv_repository.py::test_a_second_get_of_the_same_id_is_an_identity_map_hit_not_a_second_decode
    1 failed, 24 deselected in 1.14s
    ```
    Source restored byte-exact (`git diff --stat api/src` empty); re-run green alone and the full
    module green twice in a row afterward.
    """
    owner = await _persist_owner(session, clock, token_hash="30b-a" * 8)
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _upload(cvs, owner.id, clock)
    cv.mark_extracted(ExtractedText("word " * 200), clock.now())
    await cvs.add(cv)
    session.expunge_all()  # forces the first get() below to genuinely reload

    constructions = 0
    original_post_init = ExtractedText.__post_init__

    def _counting_post_init(self: ExtractedText) -> None:
        nonlocal constructions
        constructions += 1
        original_post_init(self)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ExtractedText, "__post_init__", _counting_post_init)

        first = await cvs.get(cv.id)
        second = await cvs.get(cv.id)

    assert first is second, "the second get() must hand back the exact pinned instance"
    assert constructions == 1, (
        f"ExtractedText was constructed {constructions} time(s) for two get() calls of the same id "
        "in one repository's lifetime — the second call must be an identity-map hit"
    )


async def test_count_for_session_counts_only_that_sessions_rows(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner_a = await _persist_owner(session, clock, token_hash="5" * 64)
    owner_b = await _persist_owner(session, clock, token_hash="6" * 64)
    cvs = SqlAlchemyBaseCvRepository(session)

    for owner in (owner_a, owner_a, owner_b):
        await cvs.add(_upload(cvs, owner.id, clock))

    assert await cvs.count_for_session(owner_a.id) == 2
    assert await cvs.count_for_session(owner_b.id) == 1


async def test_list_for_session_is_scoped_and_ordered_newest_first(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`list_for_session`'s docstring is explicit: newest first, so a guest who has just uploaded a
    second CV sees it above the first. A second session's rows must never appear at all (F-20's
    authorization rule depends on this same scoping)."""
    owner_a = await _persist_owner(session, clock, token_hash="7" * 64)
    owner_b = await _persist_owner(session, clock, token_hash="8" * 64)
    cvs = SqlAlchemyBaseCvRepository(session)

    first = _upload(cvs, owner_a.id, clock, filename="first.pdf")
    await cvs.add(first)

    clock.advance(60)
    second = _upload(cvs, owner_a.id, clock, filename="second.pdf")
    await cvs.add(second)

    # a different session's CV must never appear in owner_a's list, regardless of ordering
    await cvs.add(_upload(cvs, owner_b.id, clock, filename="other.pdf"))

    result = await cvs.list_for_session(owner_a.id)

    assert [item.id for item in result] == [second.id, first.id]


async def test_file_key_is_unique(session: AsyncSession, clock: FixedClock) -> None:
    """`uq_intake_base_cv_file_key` — makes "two rows, one file" unrepresentable at the database
    level (technical-plan.md's persistence table)."""
    owner = await _persist_owner(session, clock, token_hash="9" * 64)
    cvs = SqlAlchemyBaseCvRepository(session)
    first = _upload(cvs, owner.id, clock)
    await cvs.add(first)

    # a second aggregate, deliberately given the first one's exact `FileRef` — the scenario the
    # unique constraint exists to make impossible, since ordinarily `FileRef.for_base_cv` derives a
    # distinct key from a distinct id.
    duplicate_id = cvs.next_identity()
    duplicate = BaseCv.upload(
        id=duplicate_id,
        owner=GuestOwner(owner.id),
        original_filename=OriginalFilename("dup.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=1,
        file=first.file,
        uploaded_at=clock.now(),
    )
    session.add(duplicate)

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_size_bytes_positive_check_constraint(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`ck_intake_base_cv_size_bytes_positive`. `BaseCv.upload()` already refuses `size_bytes <= 0`
    at the domain level (I-1), so hitting this constraint means going around the aggregate entirely
    and inserting the row by hand — proving the database itself, not merely the domain, refuses it
    (defence in depth, matching `LocalFileStore`'s containment check).

    Every column below is `TypeDecorator`-backed, so the values handed to `.values(...)` are the
    domain types each decorator's `process_bind_param` expects (`BaseCvId`, `CvContentType`, …) —
    not the bare primitives underneath them, the same lesson `test_schema.py`'s cascade test
    needed."""
    owner = await _persist_owner(session, clock, token_hash="0" * 64)
    bad_cv_id = BaseCvId(uuid7())

    with pytest.raises(IntegrityError):
        await session.execute(
            base_cv_table.insert().values(
                id=bad_cv_id,
                guest_session_id=owner.id,
                original_filename=OriginalFilename("cv.pdf"),
                content_type=CvContentType.PDF,
                size_bytes=0,
                file_key=FileRef.for_base_cv(bad_cv_id, CvContentType.PDF),
                status=BaseCvStatus.UPLOADED,
                extracted_text=None,
                extraction_failure_reason=None,
                uploaded_at=clock.now(),
                extracted_at=None,
            )
        )


# --- Slice 2.2 (T15): the `UserOwner` half of the owner round-trip -------------------------------


async def test_round_trip_of_a_user_owned_cv_preserves_owner_and_label(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The `UserOwner` mirror of `test_round_trip_of_an_extracted_cv_preserves_value_object_types`
    above: a saved CV's `owner` and `label` survive a genuine reload as their own value-object
    types, not as the bare UUID/string the columns hold."""
    user_id = await _persist_user(session, clock, email="round-trip@example.com")
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _saved_upload(cvs, user_id, clock)
    cv.rename(BaseCvLabel("Backend roles"), at=clock.now())
    await cvs.add(cv)

    session.expunge_all()
    reloaded = await cvs.get(cv.id)

    assert isinstance(reloaded.owner, UserOwner)
    assert reloaded.owner == UserOwner(user_id)
    assert isinstance(reloaded.label, BaseCvLabel)
    assert reloaded.label == BaseCvLabel("Backend roles")


async def test_round_trip_of_a_user_owned_cv_with_no_label_reloads_label_as_none(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await _persist_user(session, clock, email="no-label@example.com")
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _saved_upload(cvs, user_id, clock)
    await cvs.add(cv)

    session.expunge_all()
    reloaded = await cvs.get(cv.id)

    assert reloaded.label is None
    assert isinstance(reloaded.owner, UserOwner)


async def test_round_trip_of_a_working_copy_preserves_its_copied_from_column(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`copied_from_base_cv_id` (I-8): a working copy — guest-owned, built via `BaseCv.copy_from` —
    reloads with `copied_from` equal to its saved source's id, as a `BaseCvId`, not a bare UUID."""
    user_id = await _persist_user(session, clock, email="copy-source@example.com")
    cvs = SqlAlchemyBaseCvRepository(session)
    source = _saved_upload(cvs, user_id, clock)
    source.mark_extracted(ExtractedText(_NON_ASCII_TEXT), at=clock.now())
    await cvs.add(source)

    guest = await _persist_owner(session, clock, token_hash="5e" * 32)
    copy_id = cvs.next_identity()
    copy = BaseCv.copy_from(
        source=source,
        id=copy_id,
        into=GuestOwner(guest.id),
        file=FileRef.for_base_cv(copy_id, source.content_type),
        at=clock.now(),
    )
    await cvs.add(copy)

    session.expunge_all()
    reloaded = await cvs.get(copy.id)

    assert isinstance(reloaded.copied_from, BaseCvId)
    assert reloaded.copied_from == source.id
    assert isinstance(reloaded.owner, GuestOwner)
    # the source itself is untouched and carries no `copied_from` of its own (I-9)
    session.expunge_all()
    reloaded_source = await cvs.get(source.id)
    assert reloaded_source.copied_from is None


# --- Slice 2.2 (T15): `list_for_user` / `count_for_user` -----------------------------------------


async def test_list_for_user_returns_newest_first_with_id_desc_tiebreak_and_excludes_other_owners(
    session: AsyncSession, clock: FixedClock
) -> None:
    """Mirrors `test_list_for_session_is_scoped_and_ordered_newest_first` above, for the `user_id`
    half: newest `uploaded_at` first, **and** — the one rule that file has no analogue of — a same-
    second tie breaks on `id DESC` (a UUIDv7 minted later sorts higher), never on whatever order
    Postgres happens to return equal timestamps in. Neither another user's saved CV nor a guest's
    workspace CV appears."""
    user = await _persist_user(session, clock, email="listed@example.com")
    other_user = await _persist_user(session, clock, email="other@example.com")
    guest = await _persist_owner(session, clock, token_hash="1a" * 32)
    cvs = SqlAlchemyBaseCvRepository(session)

    # Two saved CVs at the *same* `uploaded_at` (the clock is never advanced between them): their
    # ids alone decide the order, and `next_identity()` mints strictly increasing UUIDv7s, so the
    # first-created (`same_second_first`) has the smaller id and must sort *after* the second.
    same_second_first = _saved_upload(cvs, user, clock, filename="a.pdf")
    await cvs.add(same_second_first)
    same_second_second = _saved_upload(cvs, user, clock, filename="b.pdf")
    await cvs.add(same_second_second)
    assert same_second_first.id.value < same_second_second.id.value, (
        "test setup: the second mint must sort after the first for the tiebreak to be meaningful"
    )

    clock.advance(3600)
    newest = _saved_upload(cvs, user, clock, filename="c.pdf")
    await cvs.add(newest)

    # decoys: another user's saved CV, and a guest's workspace CV — neither must appear
    await cvs.add(_saved_upload(cvs, other_user, clock, filename="decoy.pdf"))
    await cvs.add(_upload(cvs, guest.id, clock, filename="guest.pdf"))

    result = await cvs.list_for_user(user)

    assert [item.id for item in result] == [
        newest.id,
        same_second_second.id,
        same_second_first.id,
    ]
    assert all(isinstance(item, SavedBaseCvSummary) for item in result)


async def test_list_for_user_character_count_counts_code_points_not_utf8_bytes(
    session: AsyncSession, clock: FixedClock
) -> None:
    """AC-1 (T13b's amendment): `character_count` is computed by Postgres' `char_length`, which
    counts code points — the same unit as Python's `len` and `ExtractedText.character_count` — so it
    must not silently regress to `octet_length` (UTF-8 bytes), which would disagree on this fixture."""
    user = await _persist_user(session, clock, email="nonascii@example.com")
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _saved_upload(cvs, user, clock)
    text = ExtractedText(_NON_ASCII_TEXT)
    cv.mark_extracted(text, at=clock.now())
    await cvs.add(cv)

    assert len(_NON_ASCII_TEXT) != len(_NON_ASCII_TEXT.encode("utf-8"))

    result = await cvs.list_for_user(user)

    assert len(result) == 1
    assert result[0].character_count == len(_NON_ASCII_TEXT) == text.character_count


async def test_list_for_user_reports_none_character_count_for_an_undecided_or_failed_cv(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await _persist_user(session, clock, email="undecided@example.com")
    cvs = SqlAlchemyBaseCvRepository(session)
    undecided = _saved_upload(cvs, user, clock, filename="undecided.pdf")
    await cvs.add(undecided)
    failed = _saved_upload(cvs, user, clock, filename="failed.pdf")
    failed.mark_extraction_failed(ExtractionFailureReason.CORRUPT, at=clock.now())
    await cvs.add(failed)

    result = {item.id: item for item in await cvs.list_for_user(user)}

    assert result[undecided.id].character_count is None
    assert result[undecided.id].failure_reason is None
    assert result[failed.id].character_count is None
    assert result[failed.id].failure_reason is ExtractionFailureReason.CORRUPT


async def test_list_for_user_never_selects_extracted_text_outside_char_length(
    engine: AsyncEngine, session: AsyncSession, clock: FixedClock
) -> None:
    """AC-52. Captures the actual SQL `list_for_user` sends to Postgres (the same
    `before_cursor_execute` hook `test_tailoring_run_repository.py` uses) and asserts
    `extracted_text` appears only inside `char_length(...)` — never as a selected column in its own
    right, which is what would let a CV's text travel in a result set at all.
    """
    user = await _persist_user(session, clock, email="statement-capture@example.com")
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _saved_upload(cvs, user, clock)
    cv.mark_extracted(ExtractedText(_NON_ASCII_TEXT), at=clock.now())
    await cvs.add(cv)

    captured: list[str] = []

    def _capture(conn: object, cursor: object, statement: str, *_args: object) -> None:
        captured.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        await cvs.list_for_user(user)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    matching = [
        s for s in captured if "intake_base_cv" in s and "select" in s.lower() and "user_id" in s
    ]
    assert matching, "list_for_user's SELECT never reached the database"
    statement = matching[-1]
    assert "char_length(intake_base_cv.extracted_text)" in statement.replace('"', ""), (
        f"the character-count column is not computed via char_length(): {statement!r}"
    )
    # Strip the one legitimate occurrence and confirm no second, bare reference to the column
    # remains — the assertion that actually distinguishes "selected via char_length only" from
    # "selected outright, and char_length happens to be called on it too".
    without_the_char_length_call = statement.replace(
        "char_length(intake_base_cv.extracted_text)", ""
    ).replace('char_length("intake_base_cv".extracted_text)', "")
    assert "extracted_text" not in without_the_char_length_call, (
        f"extracted_text appears outside char_length(...) in: {statement!r}"
    )


async def test_count_for_user_counts_only_that_users_rows(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_a = await _persist_user(session, clock, email="count-a@example.com")
    user_b = await _persist_user(session, clock, email="count-b@example.com")
    cvs = SqlAlchemyBaseCvRepository(session)

    for user in (user_a, user_a, user_b):
        await cvs.add(_saved_upload(cvs, user, clock))

    assert await cvs.count_for_user(user_a) == 2
    assert await cvs.count_for_user(user_b) == 1


# --- Slice 2.2 (T15): `save_label` — Core, owner-scoped, zero rows -> BaseCvNotFound --------------


async def test_save_label_updates_only_the_owning_users_row(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await _persist_user(session, clock, email="renamer@example.com")
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _saved_upload(cvs, user, clock)
    await cvs.add(cv)

    cv.rename(BaseCvLabel("Updated label"), at=clock.now())
    await cvs.save_label(cv)

    session.expunge_all()
    reloaded = await cvs.get(cv.id)
    assert reloaded.label == BaseCvLabel("Updated label")


async def test_save_label_raises_base_cv_not_found_for_a_guest_owned_row(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`save_label`'s `WHERE id = :id AND <owner>` is the owner check *and* the write in one
    statement (the module docstring's whole point): a `BaseCv` whose in-memory `owner` the caller
    forged does not match what is actually stored, so the `UPDATE` matches zero rows."""
    guest = await _persist_owner(session, clock, token_hash="2b" * 32)
    cvs = SqlAlchemyBaseCvRepository(session)
    guest_cv = _upload(cvs, guest.id, clock)
    await cvs.add(guest_cv)

    # A `BaseCv` reporting a `UserOwner` for a row that is actually guest-owned in the database —
    # exactly what a caller handed the wrong aggregate, or a race, would produce.
    forged_owner_id = UserId(value=uuid7())
    forged = BaseCv.upload(
        id=guest_cv.id,
        owner=UserOwner(forged_owner_id),
        original_filename=guest_cv.original_filename,
        content_type=guest_cv.content_type,
        size_bytes=guest_cv.size_bytes,
        file=guest_cv.file,
        uploaded_at=guest_cv.uploaded_at,
    )
    forged.rename(BaseCvLabel("should not land"), at=clock.now())

    with pytest.raises(BaseCvNotFound):
        await cvs.save_label(forged)


async def test_save_label_raises_base_cv_not_found_when_the_row_is_already_gone(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await _persist_user(session, clock, email="gone-row@example.com")
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _saved_upload(cvs, user, clock)
    await cvs.add(cv)
    await cvs.remove(cv.id, UserOwner(user))

    cv.rename(BaseCvLabel("too late"), at=clock.now())
    with pytest.raises(BaseCvNotFound):
        await cvs.save_label(cv)


# --- Slice 2.2 (T15): `remove` — Core, owner-scoped, zero rows -> BaseCvNotFound -------------------


async def test_remove_deletes_only_the_owning_users_row(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await _persist_user(session, clock, email="deleter@example.com")
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _saved_upload(cvs, user, clock)
    await cvs.add(cv)

    await cvs.remove(cv.id, UserOwner(user))

    with pytest.raises(BaseCvNotFound):
        await cvs.get(cv.id)


async def test_remove_raises_base_cv_not_found_for_a_guest_owned_id(
    session: AsyncSession, clock: FixedClock
) -> None:
    """A guest-owned id handed to `remove` (which always takes a `UserOwner`) matches zero rows —
    the type signature already says "guest rows are not removed by a request" (technical plan §1),
    and this proves the `WHERE` agrees at runtime."""
    guest = await _persist_owner(session, clock, token_hash="3c" * 32)
    cvs = SqlAlchemyBaseCvRepository(session)
    guest_cv = _upload(cvs, guest.id, clock)
    await cvs.add(guest_cv)

    with pytest.raises(BaseCvNotFound):
        await cvs.remove(guest_cv.id, UserOwner(UserId(value=uuid7())))

    # untouched: still there, under its real owner
    session.expunge_all()
    reloaded = await cvs.get(guest_cv.id)
    assert reloaded.owner == GuestOwner(guest.id)


async def test_remove_raises_base_cv_not_found_on_a_second_call(
    session: AsyncSession, clock: FixedClock
) -> None:
    user = await _persist_user(session, clock, email="twice-deleted@example.com")
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _saved_upload(cvs, user, clock)
    await cvs.add(cv)

    await cvs.remove(cv.id, UserOwner(user))
    with pytest.raises(BaseCvNotFound):
        await cvs.remove(cv.id, UserOwner(user))


# --- Slice 2.2 (T15): `add` for a user that does not exist -> UserNotFound, by name, never message -


async def test_add_for_a_nonexistent_user_raises_user_not_found_via_violated_constraint(
    session: AsyncSession, clock: FixedClock
) -> None:
    """S-12 / AC-32's translation, proven at the repository: `fk_intake_base_cv_user_id_identity_user`
    refuses an insert naming a user id nobody persisted, and `add` recognises that refusal **by the
    constraint's name** (never by parsing the driver's message, which `handle_error` withholds
    anyway) and re-raises it as `UserNotFound`. The session must still be usable afterwards — the
    whole reason `add` runs inside a SAVEPOINT."""
    cvs = SqlAlchemyBaseCvRepository(session)
    nonexistent_user = UserId(value=uuid7())
    cv = _saved_upload(cvs, nonexistent_user, clock)

    with pytest.raises(UserNotFound):
        await cvs.add(cv)

    # the SAVEPOINT contained the failure: an ordinary write in the same session still works
    guest = await _persist_owner(session, clock, token_hash="4d" * 32)
    await cvs.add(_upload(cvs, guest.id, clock))
