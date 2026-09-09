"""Domain events for `posting`: what `JobPosting` records, and what it must never carry (AC-19).

Pure domain tests: no I/O, no fixtures, no event loop, no mocks.

The field-set assertion at the bottom is the whole of AC-19, and it is worth being precise about
what it is guarding. `LoggingEventPublisher` logs *every field of every event it receives*, so
**an event's field set is a log field set** — adding a field to `JobPostingCaptured` is the same act
as adding a column to a log line, in a product where the sensitive fact is not the job description
but the *pairing*: "session X captured this posting" records which job a named person is applying
for, next to a CV carrying their name and address (Constitution §8).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import UUID

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.posting.events import JobPostingCaptured
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import (
    FetchedPosting,
    JobPostingId,
    JobPostingText,
    PostingSource,
    PostingTitle,
    SourceUrl,
)

_POSTING_ID = JobPostingId(value=UUID("0192f0a1-89ab-7cde-8123-456789abcdef"))
_SESSION_ID = GuestSessionId(value=UUID("11111111-1111-7111-8111-111111111111"))
_CREATED_AT = datetime(2026, 9, 9, 10, 0, 0, tzinfo=UTC)  # whole-second, ADR-0007
_URL = SourceUrl("https://boards.example.com/jobs/4821")
_TITLE = PostingTitle("Senior Python Engineer")


def _posting_text() -> JobPostingText:
    """150 non-whitespace characters, 174 characters of normalized length — see the same helper in
    `test_job_posting.py`. The two counts differ deliberately, which is what lets the tests below
    tell which of them `character_count` reports.

    Duplicated across the two modules rather than lifted into a shared helper, exactly as `intake`
    duplicates `_upload` / `_uploaded_cv`: a reader lands in one file, and a fixture module would
    cost them a second file to understand for four lines of setup.
    """
    return JobPostingText(" ".join(["python"] * 25))


# --- what gets recorded, and when ----------------------------------------------------------------


def test_from_pasted_text_records_exactly_one_job_posting_captured() -> None:
    """One event per constructor — and `character_count` is the **normalized length** (174 here),
    not the non-whitespace count (150).

    That number is asserted as a literal on purpose. It is the same quantity the API returns and
    the UI counter divides by 30,000 (`3,184 / 30,000`), which is the reading the 2026-09-09
    decision fixed for the ceiling; an event carrying the other count would put a third, quietly
    different "how long is it" number into circulation. And it is the reason the event can exist at
    all without the text: `character_count` lives on `JobPostingText` precisely so a subscriber
    reporting on how much text there is never has to hold any of it.
    """
    posting = JobPosting.from_pasted_text(
        id=_POSTING_ID,
        guest_session_id=_SESSION_ID,
        text=_posting_text(),
        created_at=_CREATED_AT,
    )

    events = posting.release_events()

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, JobPostingCaptured)
    assert event.job_posting_id == _POSTING_ID
    assert event.guest_session_id == _SESSION_ID
    assert event.source is PostingSource.PASTED
    assert event.character_count == 174
    assert event.occurred_at == _CREATED_AT


def test_from_fetched_url_records_exactly_one_job_posting_captured() -> None:
    """The same single event, with `source` telling the two apart.

    **One event, not two.** `JobPostingPasted` / `JobPostingFetched` would make every future
    subscriber — 1.3's tailoring trigger first — handle two names for one fact, and each new
    subscriber would be one more place to forget the second name. There is one fact here, *a
    posting now exists for this session*; `source` is a field of it. A subscriber that cares
    branches on the field, and the common case, which does not care, ignores it.
    """
    posting = JobPosting.from_fetched_url(
        id=_POSTING_ID,
        guest_session_id=_SESSION_ID,
        url=_URL,
        fetched=FetchedPosting(text=_posting_text(), title=_TITLE),
        created_at=_CREATED_AT,
    )

    events = posting.release_events()

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, JobPostingCaptured)
    assert event.source is PostingSource.FETCHED
    assert event.character_count == 174
    assert event.occurred_at == _CREATED_AT


# --- release_events empties the buffer ------------------------------------------------------------


def test_release_events_empties_the_buffer() -> None:
    """Called twice, the second call returns nothing — this is what stops a retried handler from
    publishing the same fact twice (`RecordsEvents.release_events`)."""
    posting = JobPosting.from_pasted_text(
        id=_POSTING_ID,
        guest_session_id=_SESSION_ID,
        text=_posting_text(),
        created_at=_CREATED_AT,
    )

    first_release = posting.release_events()
    second_release = posting.release_events()

    assert len(first_release) == 1
    assert second_release == ()


# --- AC-19: the payload carries no text, no URL and no title (green on arrival) ------------------
#
# `events.py` is pure data, so T4 wrote it whole and this test passes immediately — there is no
# behaviour here that a `NotImplementedError` could have failed. It is a regression guard aimed at a
# future edit, not at the current one.


def test_job_posting_captured_field_set_is_exactly_the_five_agreed_fields() -> None:
    """AC-19, asserted as **equality over the whole field set** rather than as the absence of a few
    forbidden names.

    The direction of the guard is the entire point. The regression this criterion fears is someone
    *adding* `text`, `source_url` or `title` to the payload — for one subscriber's convenience,
    without noticing that `LoggingEventPublisher` will then write it into every log line. A
    disjointness check against a list of names we thought of today passes forever as long as the new
    field is called something else (`posting_text`, `job_url`, `page_title`, `preview`); an equality
    check fails on *any* new field, including the one nobody predicted.

    **This deliberately differs from `tests/unit/intake/test_events.py`, which checks
    disjointness**, and the contradiction is recorded here rather than smoothed over. That module
    parametrizes one assertion across three events with different payloads, where a shared
    forbidden-name list is the only thing they have in common; here there is a single event with a
    single agreed payload, so the stronger form is available and the weaker one would be a choice to
    catch less. A reader who spots the inconsistency should find this paragraph, not "fix" one file
    to match the other.

    `occurred_at` is inherited from `DomainEvent` and belongs in the expected set — it is part of
    what gets logged, so leaving it out would make this assertion pass for the wrong reason.
    """
    field_names = {field.name for field in dataclasses.fields(JobPostingCaptured)}

    assert field_names == {
        "job_posting_id",
        "guest_session_id",
        "source",
        "character_count",
        "occurred_at",
    }
