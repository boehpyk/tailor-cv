"""RED (T8): the `FitScore` aggregate, FS-1 to FS-3 from technical-plan.md section 1 and the plan's
"Corrections made during implementation" (the constructor is `FitScore.from_assessment`).

Pure: no I/O, no fixtures. Every assertion comes from the invariant table, not from running the code.
What the aggregate *records* lives in the sibling `test_events.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
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
from tailorcraft.domain.shared.errors import InvariantViolated

_ID = FitScoreId(UUID("0192f0a1-89ab-7cde-8123-456789abcde0"))
_GUEST = GuestOwner(GuestSessionId(UUID("11111111-1111-7111-8111-111111111111")))
_USER = UserOwner(UserId(UUID("55555555-5555-7555-8555-555555555555")))
_CV_REF = ScoredCvRef(UUID("22222222-2222-7222-8222-222222222222"))
_POSTING_REF = ScoredPostingRef(UUID("33333333-3333-7333-8333-333333333333"))
_CV_FP = CvFingerprint("a" * 64)
_POSTING_FP = PostingFingerprint("b" * 64)
_COST = MicroUsd(170)
# Whole-second, per ADR-0007.
_AT = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)

_USAGE = ScoringUsage(
    input_tokens=1_200,
    output_tokens=500,
    unmetered_attempts=1,
    duration_ms=2_345,
    model=ModelName("gemini-x"),
    prompt_version=PromptVersion("1"),
)
_ASSESSMENT = FitAssessment(
    ScorePercent(62), (FitGap("Kubernetes experience"), FitGap("Terraform in production"))
)
_ASSESSED = AssessedFit(_ASSESSMENT, _USAGE)


def build(
    *,
    owner: Owner = _USER,
    assessed: AssessedFit = _ASSESSED,
    cost: MicroUsd = _COST,
    at: datetime = _AT,
) -> FitScore:
    return FitScore.from_assessment(
        id=_ID,
        owner=owner,
        base_cv_id=_CV_REF,
        job_posting_id=_POSTING_REF,
        cv_fingerprint=_CV_FP,
        posting_fingerprint=_POSTING_FP,
        assessed=assessed,
        cost=cost,
        at=at,
    )


# --- FS-1: exactly one owner --------------------------------------------------------------------


@pytest.mark.parametrize("owner", [_USER, _GUEST], ids=["user", "guest"])
def test_the_owner_is_the_one_it_was_recorded_for(owner: Owner) -> None:
    assert build(owner=owner).owner == owner


def test_a_guest_owned_score_is_not_user_owned_over_the_same_uuid() -> None:
    same = UUID("77777777-7777-7777-8777-777777777777")
    score = build(owner=GuestOwner(GuestSessionId(same)))
    assert score.owner != UserOwner(UserId(same))


def test_an_instance_cannot_be_built_through_the_mapped_attribute_names() -> None:
    """The explicit empty `__init__` (CLAUDE.md): without it `FitScore(_score=...)` skips every
    invariant. Already true on the skeleton, so it passed on arrival and is not a RED."""
    with pytest.raises(TypeError):
        FitScore(_score=ScorePercent(99))  # type: ignore[call-arg]


# --- FS-2: a valid assessment, usage, cost and provenance ---------------------------------------


def test_the_identity_and_both_refs_are_the_ones_given() -> None:
    score = build()
    assert score.id == _ID
    assert score.base_cv_id == _CV_REF
    assert score.job_posting_id == _POSTING_REF


def test_the_fingerprints_are_the_ones_given() -> None:
    score = build()
    assert score.cv_fingerprint == _CV_FP
    assert score.posting_fingerprint == _POSTING_FP


def test_the_assessment_is_the_one_the_scorer_returned() -> None:
    score = build()
    assert score.assessment == _ASSESSMENT
    assert score.assessment.score.value == 62
    assert [gap.value for gap in score.assessment.gaps] == [
        "Kubernetes experience",
        "Terraform in production",
    ]


def test_the_usage_with_its_provenance_is_the_one_the_scorer_returned() -> None:
    usage = build().usage
    assert usage == _USAGE
    assert usage.model == ModelName("gemini-x")
    assert usage.prompt_version == PromptVersion("1")


def test_the_cost_is_the_amount_given_not_recomputed_from_the_usage() -> None:
    assert build(cost=MicroUsd(9_999)).cost == MicroUsd(9_999)


def test_a_score_at_ninety_with_no_gaps_is_recorded() -> None:
    clean = AssessedFit(FitAssessment(ScorePercent(95), ()), _USAGE)
    assert build(assessed=clean).assessment.gaps == ()


def test_the_scored_instant_is_the_one_given() -> None:
    assert build().scored_at == _AT


def test_a_naive_instant_is_refused() -> None:
    with pytest.raises(InvariantViolated):
        build(at=datetime(2026, 10, 30, 12, 0, 0))


def test_a_sub_second_instant_is_refused() -> None:
    with pytest.raises(InvariantViolated):
        build(at=_AT + timedelta(microseconds=1))


def test_a_whole_second_instant_in_another_offset_is_accepted() -> None:
    plus_two = datetime(2026, 10, 30, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert build(at=plus_two).scored_at == _AT


# --- FS-3: it never changes ---------------------------------------------------------------------

_PUBLIC_API = {"from_assessment", "record", "release_events"}
_READ_ONLY = [
    "id",
    "owner",
    "base_cv_id",
    "job_posting_id",
    "cv_fingerprint",
    "posting_fingerprint",
    "assessment",
    "usage",
    "cost",
    "scored_at",
]


def test_no_mutator_exists() -> None:
    """The public surface is the constructor, the two `RecordsEvents` methods and read-only
    properties: nothing else. A method added later fails this on purpose. Already true on the
    skeleton, so it passed on arrival and is not a RED."""
    public = {name for name in dir(FitScore) if not name.startswith("_")}
    assert public - set(_READ_ONLY) == _PUBLIC_API


@pytest.mark.parametrize("name", _READ_ONLY)
def test_every_property_is_read_only(name: str) -> None:
    score = build()
    with pytest.raises(AttributeError):
        setattr(score, name, getattr(score, name))


def test_reading_the_score_twice_gives_the_same_answer() -> None:
    score = build()
    assert (score.assessment, score.usage, score.cost, score.owner) == (
        score.assessment,
        score.usage,
        score.cost,
        score.owner,
    )
    assert score.cost == _COST
