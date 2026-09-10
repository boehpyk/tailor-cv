"""Application tests for `CaptureJobPosting` (T9, RED).

**Why these fakes, not a real Postgres:** the same reason `test_upload_base_cv.py`'s module
docstring gives for 1.1 — as of this commit `infrastructure/persistence/mapping/posting/` has no
mapping module and there is no migration bringing `tailorcraft_test` to head, so a test that imported
`conftest.py`'s `session`/`engine` fixtures would fail for a reason that has nothing to do with
`CaptureJobPosting`. Writing the red honestly means testing the use case against the ports it
actually depends on: in-memory fakes of `JobPostingRepository`, `GuestSessionRepository` and
`JobPostingFetcherPort`, imported from `tests/integration/fakes.py` (shared with T12/T13's read-side
tests — see that module's docstring for why they live there rather than being copied per test
module) — each satisfying its Protocol exactly. The real persistence round-trip gets its own test
once the repositories and the migration exist.

The aggregates are not faked — `JobPosting` and `GuestSession` are the real domain classes.

Every assertion below states what `CaptureJobPosting.__call__` **should** do per
technical-plan.md's "Flow" section and feature-spec.md's failure contract, never what the (currently
`NotImplementedError`) code was observed doing.

**The assertion that matters most in this file** is
`test_every_fetch_failure_propagates_unchanged_and_creates_no_row` below. It is the deliberate
contrast with `UploadBaseCv`, which catches `CvExtractionFailed` and records it as a state of the
aggregate (ADR-0004's rule, executed in slice 1.1). `JobPostingFetchFailed` is **not** caught here —
technical-plan.md's step 5 says so explicitly, and ADR-0013 records why: a failed fetch has no
artifact, so there is nothing to record a failed state *of*. A reader who has just implemented
`UploadBaseCv`'s `try/except CvExtractionFailed` and arrives at `CaptureJobPosting` will be tempted
to "fix" the apparent omission by adding the equivalent `try/except` here and turning the failure
into some `JobPosting`-shaped recorded state. That "fix" is exactly what this test exists to turn
red: catching the exception here would make every case in the parametrize below fail with
`Failed: DID NOT RAISE <the expected subclass>`.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from tailorcraft.application.posting.capture_job_posting import (
    CaptureJobPosting,
    CaptureJobPostingResult,
    FetchJobPostingCommand,
    PasteJobPostingCommand,
)
from tailorcraft.domain.identity.errors import GuestSessionExpired, GuestSessionNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.posting.errors import (
    JobPostingFetchFailed,
    SourceHasNoReadableText,
    SourceNotHtml,
    SourceRejectedRequest,
    SourceResponseTooLarge,
    SourceTextTooLong,
    SourceTimedOut,
    SourceTooManyRedirects,
    SourceUnreachable,
    SourceUrlNotAllowed,
    TooManyJobPostings,
)
from tailorcraft.domain.posting.events import JobPostingCaptured
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import (
    FetchedPosting,
    JobPostingText,
    PostingSource,
    PostingTitle,
    SourceUrl,
)
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeGuestSessionRepository,
    FakeJobPostingFetcher,
    FakeJobPostingRepository,
    RecordingEventPublisher,
    create_active_session,
)

# --- Test helpers --------------------------------------------------------------------------------

_SOME_URL = "https://jobs.example.com/postings/1234"


def _long_enough_text(marker: str = "x") -> JobPostingText:
    """150 non-whitespace characters — comfortably past the 100-character floor and comfortably
    under the 30,000-character ceiling, so a test using this is never accidentally exercising P-5 or
    P-6."""
    return JobPostingText(marker * 150)


def _harmless_fetched_posting() -> FetchedPosting:
    """A `FetchedPosting` a paste-path test can hand to the (unused) fetcher without it meaning
    anything — the paste arm of `__call__` must never call `fetcher.fetch` at all."""
    return FetchedPosting(text=_long_enough_text("z"), title=None)


# --- 1. Happy path: paste --------------------------------------------------------------------------


async def test_pasting_returns_pasted_result_saves_and_publishes_after_the_save(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    postings = FakeJobPostingRepository()
    fetcher = FakeJobPostingFetcher(outcome=_harmless_fetched_posting())
    events = RecordingEventPublisher(repo=postings)
    use_case = CaptureJobPosting(postings, sessions, fetcher, events, clock)

    text = _long_enough_text("p")
    cmd = PasteJobPostingCommand(guest_session_id=session.id, text=text)
    result = await use_case(cmd)

    assert isinstance(result, CaptureJobPostingResult)
    assert result.source is PostingSource.PASTED
    assert result.character_count == text.character_count
    assert result.title is None

    # the pasted arm never reaches the fetcher
    assert fetcher.calls == 0

    # the posting is actually saved, and carries the right owner
    stored = await postings.get(result.job_posting_id)
    assert stored.guest_session_id == session.id
    assert stored.source is PostingSource.PASTED
    assert stored.source_url is None
    assert stored.title is None
    assert stored.text == text

    # exactly one JobPostingCaptured, published after the save (not before)
    assert len(events.published) == 1
    event = events.published[0]
    assert isinstance(event, JobPostingCaptured)
    assert event.job_posting_id == result.job_posting_id
    assert event.guest_session_id == session.id
    assert event.source is PostingSource.PASTED
    assert event.character_count == text.character_count
    # positive evidence of ordering: the row already existed when publish() first ran
    assert events.repo_size_at_first_publish == 1


# --- 2. Happy path: fetch --------------------------------------------------------------------------


async def test_fetching_returns_fetched_result_saves_and_publishes_after_the_save(
    clock: FixedClock,
) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    postings = FakeJobPostingRepository()
    fetched_text = _long_enough_text("f")
    fetched_title = PostingTitle("Senior Python Engineer")
    fetcher = FakeJobPostingFetcher(outcome=FetchedPosting(text=fetched_text, title=fetched_title))
    events = RecordingEventPublisher(repo=postings)
    use_case = CaptureJobPosting(postings, sessions, fetcher, events, clock)

    url = SourceUrl(_SOME_URL)
    cmd = FetchJobPostingCommand(guest_session_id=session.id, url=url)
    result = await use_case(cmd)

    assert isinstance(result, CaptureJobPostingResult)
    assert result.source is PostingSource.FETCHED
    assert result.character_count == fetched_text.character_count
    assert result.title == fetched_title
    assert fetcher.calls == 1

    stored = await postings.get(result.job_posting_id)
    assert stored.guest_session_id == session.id
    assert stored.source is PostingSource.FETCHED
    assert stored.source_url == url
    assert stored.title == fetched_title
    assert stored.text == fetched_text

    assert len(events.published) == 1
    event = events.published[0]
    assert isinstance(event, JobPostingCaptured)
    assert event.job_posting_id == result.job_posting_id
    assert event.guest_session_id == session.id
    assert event.source is PostingSource.FETCHED
    assert event.character_count == fetched_text.character_count
    assert events.repo_size_at_first_publish == 1


# --- 3. Every JobPostingFetchFailed subclass propagates unchanged, and creates no row --------------

_FETCH_FAILURE_CASES = [
    pytest.param(SourceUrlNotAllowed(), id="P-11-blocked_target"),
    pytest.param(SourceUnreachable(), id="P-13/14-unreachable"),
    pytest.param(SourceTimedOut(), id="P-15-timed_out"),
    pytest.param(SourceRejectedRequest(), id="P-16-rejected"),
    pytest.param(SourceTooManyRedirects(), id="P-17-too_many_redirects"),
    pytest.param(SourceResponseTooLarge(), id="P-19-response_too_large"),
    pytest.param(SourceNotHtml(), id="P-20-not_html"),
    pytest.param(SourceHasNoReadableText(), id="P-21/22-no_readable_text"),
    pytest.param(SourceTextTooLong(), id="P-24-text_too_long"),
]


@pytest.mark.parametrize("failure", _FETCH_FAILURE_CASES)
async def test_every_fetch_failure_propagates_unchanged_and_creates_no_row(
    clock: FixedClock, failure: JobPostingFetchFailed
) -> None:
    """AC-12: a failed fetch creates no `JobPosting` and publishes no event. See this module's
    docstring for why `pytest.raises(type(failure))` — the exact subclass, not the `JobPostingFetchFailed`
    base — is the point of this test: technical-plan.md's step 5 says the use case does not catch
    this exception at all, so the type that leaves `fetcher.fetch` must be the type that leaves
    `use_case()` unchanged."""
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    postings = FakeJobPostingRepository()
    fetcher = FakeJobPostingFetcher(outcome=failure)
    events = RecordingEventPublisher()
    use_case = CaptureJobPosting(postings, sessions, fetcher, events, clock)

    cmd = FetchJobPostingCommand(guest_session_id=session.id, url=SourceUrl(_SOME_URL))

    with pytest.raises(type(failure)):
        await use_case(cmd)

    # AC-12, asserted positively: nothing was added to the repository
    assert len(postings.all()) == 0
    assert events.published == []


# --- 4. TooManyJobPostings: the 10th succeeds, the 11th is rejected --------------------------------


async def test_tenth_job_posting_succeeds_and_eleventh_raises_too_many_job_postings(
    clock: FixedClock,
) -> None:
    """A cap tested only from the rejecting side would pass for an off-by-one that rejects the 10th
    posting too — so this test proves the 10th succeeds *before* proving the 11th does not."""
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)
    postings = FakeJobPostingRepository()

    for _ in range(9):
        existing_id = postings.next_identity()
        existing = JobPosting.from_pasted_text(
            id=existing_id,
            guest_session_id=session.id,
            text=_long_enough_text("e"),
            created_at=clock.now(),
        )
        await postings.add(existing)

    fetcher = FakeJobPostingFetcher(outcome=_harmless_fetched_posting())
    events = RecordingEventPublisher()
    use_case = CaptureJobPosting(postings, sessions, fetcher, events, clock, max_per_session=10)

    tenth_cmd = PasteJobPostingCommand(guest_session_id=session.id, text=_long_enough_text("t"))
    tenth_result = await use_case(tenth_cmd)

    assert isinstance(tenth_result, CaptureJobPostingResult)
    assert len(postings.all()) == 10

    eleventh_cmd = PasteJobPostingCommand(guest_session_id=session.id, text=_long_enough_text("v"))

    with pytest.raises(TooManyJobPostings):
        await use_case(eleventh_cmd)

    # the cap is checked before a new posting is minted — still 10, no eleventh row
    assert len(postings.all()) == 10


# --- 5. Expired / missing guest session -------------------------------------------------------------


async def test_expired_guest_session_raises_guest_session_expired(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    expired = GuestSession.start(
        id=sessions.next_identity(),
        token_hash="expired-session-token-hash".ljust(64, "0"),
        at=clock.now() - timedelta(hours=48),
        ttl_hours=24,
    )
    await sessions.add(expired)
    postings = FakeJobPostingRepository()
    fetcher = FakeJobPostingFetcher(outcome=_harmless_fetched_posting())
    events = RecordingEventPublisher()
    use_case = CaptureJobPosting(postings, sessions, fetcher, events, clock)

    cmd = PasteJobPostingCommand(guest_session_id=expired.id, text=_long_enough_text())

    with pytest.raises(GuestSessionExpired):
        await use_case(cmd)

    assert postings.all() == []
    assert events.published == []


async def test_missing_guest_session_raises_guest_session_not_found(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()  # empty: no session was ever added
    postings = FakeJobPostingRepository()
    fetcher = FakeJobPostingFetcher(outcome=_harmless_fetched_posting())
    events = RecordingEventPublisher()
    use_case = CaptureJobPosting(postings, sessions, fetcher, events, clock)

    unknown_session_id = GuestSessionId(value=uuid4())
    cmd = PasteJobPostingCommand(guest_session_id=unknown_session_id, text=_long_enough_text())

    with pytest.raises(GuestSessionNotFound):
        await use_case(cmd)

    assert postings.all() == []
    assert events.published == []
