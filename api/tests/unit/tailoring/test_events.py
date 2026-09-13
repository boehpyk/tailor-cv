"""Domain events for `tailoring`: what `TailoringRun` records, and what it must never carry (AC-22).

Pure domain tests: no I/O, no fixtures, no event loop, no mocks.

The field-set assertions at the bottom are the whole of AC-22, and this is the context where they
matter most in the entire codebase. `LoggingEventPublisher` logs *every field of every event it
receives*, so an event's field set *is* a log field set — adding a field to `TailoringRunSucceeded`
or `TailoringRunFailed` is the same act as adding a column to a log line, in the one slice whose
whole job is to turn a person's employment history into more text. AC-22 is checked structurally,
via `dataclasses.fields()`, rather than by grepping for one string that might be in there: a test
that only asserted "the CV string isn't in the event" would pass vacuously the day someone adds a
field nobody predicted (`draft_preview`, `model_output`, `first_paragraph`, ...).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.domain.tailoring.events import (
    TailoringRunFailed,
    TailoringRunRequested,
    TailoringRunStarted,
    TailoringRunSucceeded,
)
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocuments,
    TailoringFailureReason,
    TailoringRunId,
)

_RUN_ID = TailoringRunId(value=UUID("0192f0a1-89ab-7cde-8123-456789abcde0"))
_SESSION_ID = GuestSessionId(value=UUID("11111111-1111-7111-8111-111111111111"))
_BASE_CV_ID = BaseCvId(value=UUID("22222222-2222-7222-8222-222222222222"))
_JOB_POSTING_ID = JobPostingId(value=UUID("33333333-3333-7333-8333-333333333333"))
_REQUESTED_AT = datetime(2026, 9, 10, 10, 0, 0, tzinfo=UTC)  # whole-second, ADR-0007
_STARTED_AT = _REQUESTED_AT + timedelta(seconds=5)
_COMPLETED_AT = _STARTED_AT + timedelta(seconds=20)


def _requested() -> TailoringRun:
    return TailoringRun.request(
        id=_RUN_ID,
        guest_session_id=_SESSION_ID,
        base_cv_id=_BASE_CV_ID,
        job_posting_id=_JOB_POSTING_ID,
        requested_at=_REQUESTED_AT,
    )


def _documents() -> TailoredDocuments:
    return TailoredDocuments(cv=TailoredCv("a" * 400), cover_letter=CoverLetter("a" * 200))


def _metrics() -> LlmCallMetrics:
    return LlmCallMetrics(
        model=ModelName("gemini-2.5-flash"),
        prompt_version=PromptVersion("v1"),
        prompt_tokens=1_200,
        completion_tokens=800,
        duration_ms=4_300,
    )


# --- what gets recorded, and when ------------------------------------------------------------------


def test_request_records_exactly_one_tailoring_run_requested() -> None:
    run = _requested()

    events = run.release_events()

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, TailoringRunRequested)
    assert event.tailoring_run_id == _RUN_ID
    assert event.guest_session_id == _SESSION_ID
    assert event.base_cv_id == _BASE_CV_ID
    assert event.job_posting_id == _JOB_POSTING_ID
    assert event.occurred_at == _REQUESTED_AT


def test_mark_started_records_exactly_one_tailoring_run_started() -> None:
    run = _requested()
    run.release_events()  # discard TailoringRunRequested so this test sees only what mark_started adds

    run.mark_started(_STARTED_AT)
    events = run.release_events()

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, TailoringRunStarted)
    assert event.tailoring_run_id == _RUN_ID
    assert event.occurred_at == _STARTED_AT


def test_mark_succeeded_records_one_event_carrying_the_metrics_and_the_character_counts() -> None:
    """The event carries the five metrics and the two character counts and **not** the documents
    (AC-22): `cv_character_count`/`cover_letter_character_count` exist on `TailoredCv`/`CoverLetter`
    for exactly this reason — so a subscriber reporting on what a call cost and how much text it
    produced never has to hold the text itself."""
    run = _requested()
    run.mark_started(_STARTED_AT)
    run.release_events()
    documents = _documents()
    metrics = _metrics()

    run.mark_succeeded(documents, metrics, _COMPLETED_AT)
    events = run.release_events()

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, TailoringRunSucceeded)
    assert event.tailoring_run_id == _RUN_ID
    assert event.model == metrics.model
    assert event.prompt_version == metrics.prompt_version
    assert event.prompt_tokens == metrics.prompt_tokens
    assert event.completion_tokens == metrics.completion_tokens
    assert event.duration_ms == metrics.duration_ms
    assert event.cv_character_count == documents.cv.character_count
    assert event.cover_letter_character_count == documents.cover_letter.character_count
    assert event.occurred_at == _COMPLETED_AT


def test_mark_failed_records_exactly_one_tailoring_run_failed_carrying_the_reason() -> None:
    run = _requested()
    run.release_events()

    run.mark_failed(TailoringFailureReason.NOT_QUEUED, _REQUESTED_AT)
    events = run.release_events()

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, TailoringRunFailed)
    assert event.tailoring_run_id == _RUN_ID
    assert event.reason is TailoringFailureReason.NOT_QUEUED
    assert event.occurred_at == _REQUESTED_AT


def test_mark_failed_records_the_reason_even_for_a_failure_nothing_raises() -> None:
    """`NOT_QUEUED` and `ABANDONED` have no `TailoringFailed` exception subclass — they are written
    by our own orchestration rather than raised by a port (see `TailoringFailureReason`'s docstring).
    The event still fires for both: it records the fact of a failure, not the fact of an exception."""
    run = _requested()
    run.mark_started(_STARTED_AT)
    run.release_events()

    run.mark_failed(TailoringFailureReason.ABANDONED, _COMPLETED_AT)
    events = run.release_events()

    assert len(events) == 1
    assert events[0].reason is TailoringFailureReason.ABANDONED  # type: ignore[attr-defined]


# --- one event per transition, in order -------------------------------------------------------------


def test_full_success_lifecycle_records_the_three_events_in_order() -> None:
    """1.4's progress display and 2.3's history both read this ordering back — see
    `domain/tailoring/events.py`'s "Four events, not one" for why they are four separate facts rather
    than one `TailoringRunStateChanged`."""
    run = _requested()

    run.mark_started(_STARTED_AT)
    run.mark_succeeded(_documents(), _metrics(), _COMPLETED_AT)
    events = run.release_events()

    assert [type(event) for event in events] == [
        TailoringRunRequested,
        TailoringRunStarted,
        TailoringRunSucceeded,
    ]


def test_full_failure_lifecycle_records_the_three_events_in_order() -> None:
    run = _requested()

    run.mark_started(_STARTED_AT)
    run.mark_failed(TailoringFailureReason.LLM_ERROR, _COMPLETED_AT)
    events = run.release_events()

    assert [type(event) for event in events] == [
        TailoringRunRequested,
        TailoringRunStarted,
        TailoringRunFailed,
    ]


def test_failed_before_starting_records_only_the_two_events_in_order() -> None:
    """The `queued` → `failed` path (G-14) skips `TailoringRunStarted` entirely — a run that never
    started must not manufacture a fact saying it did."""
    run = _requested()

    run.mark_failed(TailoringFailureReason.NOT_QUEUED, _REQUESTED_AT)
    events = run.release_events()

    assert [type(event) for event in events] == [TailoringRunRequested, TailoringRunFailed]


# --- release_events empties the buffer --------------------------------------------------------------


def test_release_events_empties_the_buffer() -> None:
    """Called twice, the second call returns nothing — this is what stops a retried handler from
    publishing the same fact twice (`RecordsEvents.release_events`)."""
    run = _requested()

    first_release = run.release_events()
    second_release = run.release_events()

    assert len(first_release) == 1
    assert second_release == ()


# --- AC-22: no event may carry a document body, CV text, posting text or a prompt ------------------
#
# `events.py` is pure data, written whole at the skeleton step (T4) exactly as `posting/events.py`
# was — there is no behaviour here a `NotImplementedError` could have failed, so every test in this
# section passes immediately. They are regression guards aimed at a future edit, not the current one.


def test_tailoring_run_requested_field_set_is_exactly_the_agreed_fields() -> None:
    field_names = {field.name for field in dataclasses.fields(TailoringRunRequested)}

    assert field_names == {
        "tailoring_run_id",
        "guest_session_id",
        "base_cv_id",
        "job_posting_id",
        "occurred_at",
    }


def test_tailoring_run_started_field_set_is_exactly_the_agreed_fields() -> None:
    """Deliberately sparse — this event's whole value is its timestamp, paired with the requested
    and succeeded events to measure queue latency and wall-clock duration."""
    field_names = {field.name for field in dataclasses.fields(TailoringRunStarted)}

    assert field_names == {"tailoring_run_id", "occurred_at"}


def test_tailoring_run_succeeded_field_set_is_exactly_the_agreed_fields() -> None:
    """The pairing that is this slice's whole design, asserted as an exact set: counts, never the
    documents. A future edit that adds `tailored_cv` or `cover_letter` "for convenience" is exactly
    what this assertion exists to catch."""
    field_names = {field.name for field in dataclasses.fields(TailoringRunSucceeded)}

    assert field_names == {
        "tailoring_run_id",
        "model",
        "prompt_version",
        "prompt_tokens",
        "completion_tokens",
        "duration_ms",
        "cv_character_count",
        "cover_letter_character_count",
        "occurred_at",
    }


def test_tailoring_run_failed_field_set_is_exactly_the_agreed_fields() -> None:
    """No provider message, no raw response, no input text — the closed `TailoringFailureReason`
    enum is the entire vocabulary a dashboard needs, and a free-text field could only ever leak."""
    field_names = {field.name for field in dataclasses.fields(TailoringRunFailed)}

    assert field_names == {"tailoring_run_id", "reason", "occurred_at"}


@pytest.mark.parametrize(
    "event_type",
    [TailoringRunRequested, TailoringRunStarted, TailoringRunSucceeded, TailoringRunFailed],
    ids=["Requested", "Started", "Succeeded", "Failed"],
)
def test_no_event_field_set_contains_a_document_body_or_free_text_field(
    event_type: type[DomainEvent],
) -> None:
    """The disjointness form, on top of the four exact-set assertions above — the same pairing
    `tests/unit/intake/test_events.py` uses for the same reason: the exact-set assertions catch *any*
    new field, and this one names, for a human reading this file, exactly which strings would be the
    disaster if one showed up."""
    field_names = {field.name for field in dataclasses.fields(event_type)}

    forbidden_names = {
        "tailored_cv",
        "cover_letter",
        "documents",
        "cv_text",
        "extracted_text",
        "posting_text",
        "job_posting_text",
        "prompt",
        "completion",
        "response",
        "raw_response",
        "message",
    }

    assert field_names.isdisjoint(forbidden_names)
