"""The `FitScore` aggregate: one owner's stored assessment of one pair."""

from __future__ import annotations

from datetime import datetime
from typing import assert_never

from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.scoring.events import FitScoreRecorded
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
from tailorcraft.domain.shared.events import RecordsEvents


# NOT `slots=True`: imperative mapping instruments the instance `__dict__` (`TailoringRun`'s reason).
class FitScore(RecordsEvents):
    """Only a success is a row, so there is no status, no version and no mutator (ADR-0040 decision 2).

    **Invariants.**

    - **FS-1** Exactly one owner.
    - **FS-2** A fit score always has a valid assessment, usage, cost and provenance.
    - **FS-3** It never changes, except its owner by the claim's Core `UPDATE`.
    """

    _id: FitScoreId
    # The product of two nullable ids the table stores, as on `TailoringRun`, and written out again on
    # purpose: aggregates sharing a shape do not share a rule (CLAUDE.md).
    _owner_guest_session_id: GuestSessionId | None
    _owner_user_id: UserId | None
    _base_cv_id: ScoredCvRef
    _job_posting_id: ScoredPostingRef
    _cv_fingerprint: CvFingerprint
    _posting_fingerprint: PostingFingerprint
    _score: ScorePercent
    _gaps: tuple[FitGap, ...]
    _model: ModelName
    _prompt_version: PromptVersion
    _input_tokens: int
    _output_tokens: int
    _unmetered_attempts: int
    _llm_duration_ms: int
    _cost: MicroUsd
    _scored_at: datetime

    def __init__(self) -> None:
        """Takes nothing and does nothing. Build a `FitScore` with `from_assessment`.

        **Keep it, even though it looks deletable.** `registry.map_imperatively` installs a default
        constructor on a mapped class that defines none, and it accepts the mapped attribute names, so
        without this one `FitScore(_score=…)` would skip every invariant. It must not raise: a mapped
        class is built through `cls()`. `TailoringRun.__init__` carries the full account.
        """

    @classmethod
    def from_assessment(
        cls,
        *,
        id: FitScoreId,
        owner: Owner,
        base_cv_id: ScoredCvRef,
        job_posting_id: ScoredPostingRef,
        cv_fingerprint: CvFingerprint,
        posting_fingerprint: PostingFingerprint,
        assessed: AssessedFit,
        cost: MicroUsd,
        at: datetime,
    ) -> FitScore:
        """The only constructor, recording exactly one `FitScoreRecorded`. A naive or sub-second `at`
        is `InvariantViolated`.

        Not `record`, the plan's name: that is `RecordsEvents.record(event)`, which this class needs,
        and a classmethod of the same name would replace it.
        """
        # `utcoffset() is None` rather than `tzinfo is None`: a tzinfo may still decline an offset.
        if at.utcoffset() is None:
            raise InvariantViolated("a fit score's instant must be timezone-aware")
        if at.microsecond != 0:
            raise InvariantViolated("a fit score's instant must be whole-second")

        score = cls()
        score._id = id
        score._assign_owner(owner)
        score._base_cv_id = base_cv_id
        score._job_posting_id = job_posting_id
        score._cv_fingerprint = cv_fingerprint
        score._posting_fingerprint = posting_fingerprint
        score._score = assessed.assessment.score
        score._gaps = assessed.assessment.gaps
        score._model = assessed.usage.model
        score._prompt_version = assessed.usage.prompt_version
        score._input_tokens = assessed.usage.input_tokens
        score._output_tokens = assessed.usage.output_tokens
        score._unmetered_attempts = assessed.usage.unmetered_attempts
        score._llm_duration_ms = assessed.usage.duration_ms
        score._cost = cost
        score._scored_at = at
        score.record(
            FitScoreRecorded(
                occurred_at=at,
                fit_score_id=id,
                gap_count=len(assessed.assessment.gaps),
                input_tokens=assessed.usage.input_tokens,
                output_tokens=assessed.usage.output_tokens,
                cost_microusd=cost.value,
                duration_ms=assessed.usage.duration_ms,
                model=assessed.usage.model,
                prompt_version=assessed.usage.prompt_version,
            )
        )
        return score

    def _assign_owner(self, owner: Owner) -> None:
        """The only writer of the two owner attributes (FS-1): exactly one is set, by construction."""
        match owner:
            case GuestOwner(guest_session_id=guest_session_id):
                self._owner_guest_session_id = guest_session_id
                self._owner_user_id = None
            case UserOwner(user_id=user_id):
                self._owner_guest_session_id = None
                self._owner_user_id = user_id
            case _:
                assert_never(owner)

    @property
    def id(self) -> FitScoreId:
        return self._id

    @property
    def owner(self) -> Owner:
        """Rebuilds the variant from the two attributes. The states `_assign_owner` cannot produce are
        refused by the database's CHECK, so the last arm means a row written around both: loud."""
        match (self._owner_guest_session_id, self._owner_user_id):
            case (GuestSessionId() as guest_session_id, None):
                return GuestOwner(guest_session_id)
            case (None, UserId() as user_id):
                return UserOwner(user_id)
            case _:
                raise InvariantViolated("a fit score must have exactly one owner (FS-1)")

    @property
    def base_cv_id(self) -> ScoredCvRef:
        return self._base_cv_id

    @property
    def job_posting_id(self) -> ScoredPostingRef:
        return self._job_posting_id

    @property
    def cv_fingerprint(self) -> CvFingerprint:
        return self._cv_fingerprint

    @property
    def posting_fingerprint(self) -> PostingFingerprint:
        return self._posting_fingerprint

    @property
    def assessment(self) -> FitAssessment:
        return FitAssessment(self._score, self._gaps)

    @property
    def usage(self) -> ScoringUsage:
        return ScoringUsage(
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            unmetered_attempts=self._unmetered_attempts,
            duration_ms=self._llm_duration_ms,
            model=self._model,
            prompt_version=self._prompt_version,
        )

    @property
    def cost(self) -> MicroUsd:
        return self._cost

    @property
    def scored_at(self) -> datetime:
        return self._scored_at
