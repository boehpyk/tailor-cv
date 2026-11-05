"""Application tests for `ResolveScoringPair` (slice 5.1, T15, RED): two ids in, two authorized texts out.

From the spec's AC-3, AC-22, AC-31 and rows S-22 to S-24, and the plan's § 2 and *Corrections* (the
not-ready error is scoring's own, in `domain.scoring.errors`). `GetBaseCv` and `GetJobPosting` are the
real use cases over fakes, so the ownership rule is exercised rather than assumed.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from tailorcraft.domain.identity.errors import GuestSessionExpired, UserNotFound
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.errors import JobPostingNotFound
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.scoring.errors import BaseCvNotReadyForScoring
from tailorcraft.domain.scoring.value_objects import CvForScoring, PostingForScoring
from tailorcraft.domain.tailoring.errors import BaseCvNotReadyForTailoring
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.owners import (
    extraction_failed_cv,
    pasted_posting,
    seed_expired_session,
    uploaded_cv,
)
from tests.integration.scoring.support import World, extracted_cv_with

CV_TEXT = "Led a team of five engineers.\n\nShipped   Kubernetes migrations. " * 8


def _sha(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


async def _guest_with(w: World, cv: BaseCv) -> tuple[GuestOwner, JobPostingId]:
    guest = await w.guest()
    posting = pasted_posting(guest, w.earlier)
    await w.cvs.add(cv)
    await w.postings.add(posting)
    return guest, posting.id


# --- The happy path -------------------------------------------------------------------------------------


async def test_a_guests_pair_holds_both_normalized_texts_their_fingerprints_and_the_ids(
    clock: FixedClock,
) -> None:
    w = World(clock)
    guest = await w.guest()
    cv = extracted_cv_with(guest, w.earlier, CV_TEXT)
    posting = pasted_posting(guest, w.earlier)
    await w.cvs.add(cv)
    await w.postings.add(posting)

    pair = await w.resolve()(guest, cv.id, posting.id)

    assert pair.cv.value == " ".join(CV_TEXT.split())
    assert pair.posting.value == " ".join(posting.text.value.split())
    assert pair.cv_fingerprint.value == _sha(CV_TEXT)
    assert pair.posting_fingerprint.value == _sha(posting.text.value)
    assert pair.cv_id.value == cv.id.value
    assert pair.posting_id.value == posting.id.value


async def test_a_users_pair_resolves_the_same_way(clock: FixedClock) -> None:
    w = World(clock)
    user = await w.user()
    cv_id, posting_id = await w.inputs(user, cv_text=CV_TEXT)

    pair = await w.resolve()(user, cv_id, posting_id)

    assert pair.cv_fingerprint.value == _sha(CV_TEXT)
    assert pair.cv_id.value == cv_id.value


async def test_the_same_text_under_a_new_cv_id_is_the_same_fingerprint(clock: FixedClock) -> None:
    """AC-3: identity is content. A re-upload is a new row with the same words."""
    w = World(clock)
    guest = await w.guest()
    first = extracted_cv_with(guest, w.earlier, CV_TEXT)
    again = extracted_cv_with(guest, w.earlier, CV_TEXT)
    posting = pasted_posting(guest, w.earlier)
    for cv in (first, again):
        await w.cvs.add(cv)
    await w.postings.add(posting)

    one = await w.resolve()(guest, first.id, posting.id)
    two = await w.resolve()(guest, again.id, posting.id)

    assert one.cv_id != two.cv_id
    assert one.cv_fingerprint == two.cv_fingerprint
    assert one.posting_fingerprint == two.posting_fingerprint


async def test_a_different_text_is_a_different_fingerprint(clock: FixedClock) -> None:
    w = World(clock)
    guest = await w.guest()
    one = extracted_cv_with(guest, w.earlier, CV_TEXT)
    other = extracted_cv_with(guest, w.earlier, CV_TEXT + " Plus one more sentence here.")
    posting = pasted_posting(guest, w.earlier)
    for cv in (one, other):
        await w.cvs.add(cv)
    await w.postings.add(posting)

    first = await w.resolve()(guest, one.id, posting.id)
    second = await w.resolve()(guest, other.id, posting.id)

    assert first.cv_fingerprint != second.cv_fingerprint


# --- S-24: not extracted --------------------------------------------------------------------------------------


@pytest.mark.parametrize("build", [uploaded_cv, extraction_failed_cv], ids=["uploaded", "failed"])
async def test_a_cv_with_no_extracted_text_is_not_ready_for_scoring(
    clock: FixedClock, build: Callable[..., BaseCv]
) -> None:
    w = World(clock)
    guest = await w.guest()
    cv = build(guest, w.earlier)
    posting = pasted_posting(guest, w.earlier)
    await w.cvs.add(cv)
    await w.postings.add(posting)

    with pytest.raises(BaseCvNotReadyForScoring) as info:
        await w.resolve()(guest, cv.id, posting.id)

    # Scoring's own error: closing tailoring in 5.2 must not be able to move this answer.
    assert not isinstance(info.value, BaseCvNotReadyForTailoring)


# --- Authorization: S-22, S-23, AC-31 ----------------------------------------------------------------------------


async def test_a_cv_that_does_not_exist_is_not_found(clock: FixedClock) -> None:
    w = World(clock)
    guest = await w.guest()
    posting = pasted_posting(guest, w.earlier)
    await w.postings.add(posting)

    with pytest.raises(BaseCvNotFound):
        await w.resolve()(guest, BaseCvId(value=uuid4()), posting.id)


async def test_someone_elses_cv_is_not_found(clock: FixedClock) -> None:
    w = World(clock)
    mine, theirs = await w.guest("mine"), await w.guest("theirs")
    their_cv = extracted_cv_with(theirs, w.earlier, CV_TEXT)
    posting = pasted_posting(mine, w.earlier)
    await w.cvs.add(their_cv)
    await w.postings.add(posting)

    with pytest.raises(BaseCvNotFound):
        await w.resolve()(mine, their_cv.id, posting.id)


async def test_a_users_cv_is_not_found_to_a_guest(clock: FixedClock) -> None:
    w = World(clock)
    user = await w.user()
    guest = await w.guest()
    users_cv = extracted_cv_with(user, w.earlier, CV_TEXT)
    posting = pasted_posting(guest, w.earlier)
    await w.cvs.add(users_cv)
    await w.postings.add(posting)

    with pytest.raises(BaseCvNotFound):
        await w.resolve()(guest, users_cv.id, posting.id)


async def test_a_posting_that_does_not_exist_is_not_found(clock: FixedClock) -> None:
    w = World(clock)
    guest = await w.guest()
    cv = extracted_cv_with(guest, w.earlier, CV_TEXT)
    await w.cvs.add(cv)

    with pytest.raises(JobPostingNotFound):
        await w.resolve()(guest, cv.id, JobPostingId(value=uuid4()))


async def test_someone_elses_posting_is_not_found(clock: FixedClock) -> None:
    w = World(clock)
    mine, theirs = await w.guest("mine"), await w.guest("theirs")
    cv = extracted_cv_with(mine, w.earlier, CV_TEXT)
    their_posting = pasted_posting(theirs, w.earlier)
    await w.cvs.add(cv)
    await w.postings.add(their_posting)

    with pytest.raises(JobPostingNotFound):
        await w.resolve()(mine, cv.id, their_posting.id)


async def test_someone_elses_posting_is_not_found_even_when_the_cv_is_not_extracted(
    clock: FixedClock,
) -> None:
    """S-23 before S-24: the status of a CV must never be the way to learn that a posting id exists."""
    w = World(clock)
    mine, theirs = await w.guest("mine"), await w.guest("theirs")
    cv = uploaded_cv(mine, w.earlier)
    their_posting = pasted_posting(theirs, w.earlier)
    await w.cvs.add(cv)
    await w.postings.add(their_posting)

    with pytest.raises(JobPostingNotFound):
        await w.resolve()(mine, cv.id, their_posting.id)


async def test_an_expired_guest_session_is_refused(clock: FixedClock) -> None:
    w = World(clock)
    session = await seed_expired_session(w.sessions, "gone", clock.now())
    owner = GuestOwner(session.id)

    with pytest.raises(GuestSessionExpired):
        await w.resolve()(owner, BaseCvId(value=uuid4()), JobPostingId(value=uuid4()))


async def test_an_erased_user_is_refused(clock: FixedClock) -> None:
    w = World(clock)
    erased = UserOwner(UserId(value=uuid4()))

    with pytest.raises(UserNotFound):
        await w.resolve()(erased, BaseCvId(value=uuid4()), JobPostingId(value=uuid4()))


async def test_a_session_that_never_existed_is_refused_not_trusted(clock: FixedClock) -> None:
    from tailorcraft.domain.identity.errors import GuestSessionNotFound

    w = World(clock)
    stranger = GuestOwner(GuestSessionId(value=uuid4()))

    with pytest.raises(GuestSessionNotFound):
        await w.resolve()(stranger, BaseCvId(value=uuid4()), JobPostingId(value=uuid4()))


# --- AC-22's unit half: normalizing and hashing run off the event loop ---------------------------------------


async def test_normalizing_and_fingerprinting_run_off_the_event_loop(
    clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CV may be 250,000 characters, on every `POST` and `GET`: collapsing its whitespace and
    hashing it on the loop would stall every other user (`asyncio.to_thread`, plan § 0.2).

    The two pure steps are spied where they live. Each must have run, and none on this thread."""
    w = World(clock)
    guest = await w.guest()
    cv = extracted_cv_with(guest, w.earlier, CV_TEXT)
    posting = pasted_posting(guest, w.earlier)
    await w.cvs.add(cv)
    await w.postings.add(posting)
    loop_thread = threading.get_ident()
    normalized: list[int] = []
    hashed: list[int] = []

    import tailorcraft.domain.scoring.value_objects as vo

    real_collapse = vo._collapse_whitespace

    def collapse_spy(text: str) -> str:
        normalized.append(threading.get_ident())
        return real_collapse(text)

    monkeypatch.setattr(vo, "_collapse_whitespace", collapse_spy)

    def hash_spy(original: Callable[[Any], Any]) -> property:
        def fingerprint(self: Any) -> Any:
            hashed.append(threading.get_ident())
            return original(self)

        return property(fingerprint)

    for cls in (CvForScoring, PostingForScoring):
        monkeypatch.setattr(cls, "fingerprint", hash_spy(vars(cls)["fingerprint"].fget))

    await w.resolve()(guest, cv.id, posting.id)

    assert len(normalized) >= 2
    assert len(hashed) >= 2
    assert loop_thread not in normalized
    assert loop_thread not in hashed
