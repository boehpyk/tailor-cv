"""The `FitScore` aggregate: one owner's stored assessment of one pair.

SKELETON (T4): the signatures are real, the behaviour arrives at T9.
"""

from __future__ import annotations

from datetime import datetime

from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
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
        raise NotImplementedError

    @property
    def id(self) -> FitScoreId:
        raise NotImplementedError

    @property
    def owner(self) -> Owner:
        raise NotImplementedError

    @property
    def base_cv_id(self) -> ScoredCvRef:
        raise NotImplementedError

    @property
    def job_posting_id(self) -> ScoredPostingRef:
        raise NotImplementedError

    @property
    def cv_fingerprint(self) -> CvFingerprint:
        raise NotImplementedError

    @property
    def posting_fingerprint(self) -> PostingFingerprint:
        raise NotImplementedError

    @property
    def assessment(self) -> FitAssessment:
        raise NotImplementedError

    @property
    def usage(self) -> ScoringUsage:
        raise NotImplementedError

    @property
    def cost(self) -> MicroUsd:
        raise NotImplementedError

    @property
    def scored_at(self) -> datetime:
        raise NotImplementedError
