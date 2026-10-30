"""RED (T8): what `FitScore` records, and what `FitScoreRecorded` may carry.

`LoggingEventPublisher` logs every field of an event, so an event's field set is a log field set
(Constitution section 8): no owner, score, gap, quote or fingerprint (technical-plan.md section 1).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import UUID

from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.scoring.events import FitScoreRecorded
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.value_objects import (
    AssessedFit,
    CvFingerprint,
    FitAssessment,
    FitGap,
    FitScoreId,
    MicroUsd,
    ModelName,
    PostingFingerprint,
    PromptVersion,
    ScoredCvRef,
    ScoredPostingRef,
    ScorePercent,
    ScoringUsage,
)

_ID = FitScoreId(UUID("0192f0a1-89ab-7cde-8123-456789abcde0"))
_USER_UUID = UUID("55555555-5555-7555-8555-555555555555")
_CV_FP = CvFingerprint("c0ffee" + "a" * 58)
_POSTING_FP = PostingFingerprint("d00d" + "b" * 60)
_AT = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)
_GAP_TEXT = "Kubernetes experience"
_USAGE = ScoringUsage(
    input_tokens=1_200,
    output_tokens=500,
    unmetered_attempts=1,
    duration_ms=2_345,
    model=ModelName("gemini-x"),
    prompt_version=PromptVersion("1"),
)


def build(gaps: tuple[str, ...] = (_GAP_TEXT, "Terraform in production")) -> FitScore:
    assessment = FitAssessment(ScorePercent(62 if gaps else 95), tuple(FitGap(g) for g in gaps))
    return FitScore.from_assessment(
        id=_ID,
        owner=UserOwner(UserId(_USER_UUID)),
        base_cv_id=ScoredCvRef(UUID("22222222-2222-7222-8222-222222222222")),
        job_posting_id=ScoredPostingRef(UUID("33333333-3333-7333-8333-333333333333")),
        cv_fingerprint=_CV_FP,
        posting_fingerprint=_POSTING_FP,
        assessed=AssessedFit(assessment, _USAGE),
        cost=MicroUsd(170),
        at=_AT,
    )


def test_recording_a_score_records_exactly_one_event() -> None:
    events = build().release_events()
    assert len(events) == 1
    assert isinstance(events[0], FitScoreRecorded)


def test_the_events_are_released_once() -> None:
    score = build()
    score.release_events()
    assert score.release_events() == ()


def test_the_event_carries_the_facts_of_the_score() -> None:
    (event,) = build().release_events()
    assert isinstance(event, FitScoreRecorded)
    assert event.occurred_at == _AT
    assert event.fit_score_id == _ID
    assert event.gap_count == 2
    assert event.input_tokens == 1_200
    assert event.output_tokens == 500
    assert event.cost_microusd == 170
    assert event.duration_ms == 2_345
    assert event.model == ModelName("gemini-x")
    assert event.prompt_version == PromptVersion("1")


def test_the_gap_count_is_zero_for_a_score_with_no_gaps() -> None:
    (event,) = build(gaps=()).release_events()
    assert isinstance(event, FitScoreRecorded)
    assert event.gap_count == 0


def test_the_event_has_no_field_for_text_an_owner_a_fingerprint_or_an_assessment() -> None:
    """The field set is closed: adding one fails this on purpose, and the reviewer reads it.
    Already true on the skeleton's event, so it passed on arrival and is not a RED."""
    names = {field.name for field in dataclasses.fields(FitScoreRecorded)}
    assert names == {
        "occurred_at",
        "fit_score_id",
        "gap_count",
        "input_tokens",
        "output_tokens",
        "cost_microusd",
        "duration_ms",
        "model",
        "prompt_version",
    }


def test_a_recorded_event_leaks_no_gap_fingerprint_or_owner_into_its_repr() -> None:
    (event,) = build().release_events()
    shown = repr(event) + str(event)
    assert _GAP_TEXT not in shown
    assert "Terraform" not in shown
    assert _CV_FP.value not in shown
    assert _POSTING_FP.value not in shown
    assert str(_USER_UUID) not in shown


def test_no_value_of_the_event_is_a_gap_a_fingerprint_or_an_owner() -> None:
    (event,) = build().release_events()
    values = [getattr(event, field.name) for field in dataclasses.fields(event)]
    assert _CV_FP not in values
    assert _POSTING_FP not in values
    assert UserOwner(UserId(_USER_UUID)) not in values
    assert not any(isinstance(value, (FitGap, FitAssessment, ScorePercent)) for value in values)
