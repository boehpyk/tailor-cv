"""The `ResolveScoringPair` use case: turn two ids into the two texts a score judges, authorized."""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.application.intake.get_base_cv import GetBaseCv
from tailorcraft.application.posting.get_job_posting import GetJobPosting
from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.scoring.value_objects import (
    CvFingerprint,
    CvForScoring,
    PostingFingerprint,
    PostingForScoring,
    ScoredCvRef,
    ScoredPostingRef,
)


@dataclass(frozen=True, slots=True, repr=False)
class ScoringPair:
    """Everything the rest of this package needs, built once under `asyncio.to_thread` (AC-22).

    The fingerprints are fields because `CvForScoring.fingerprint` hashes on every read, and a
    250,000-character CV must not be hashed again on the event loop. `repr=False`: it holds a CV.
    """

    cv_id: ScoredCvRef
    posting_id: ScoredPostingRef
    cv: CvForScoring
    posting: PostingForScoring
    cv_fingerprint: CvFingerprint
    posting_fingerprint: PostingFingerprint


class ResolveScoringPair:
    """Authorize both ids against `owner` and build the `ScoringPair`.

    Takes two use cases, not two repositories, for `RequestTailoringRun`'s reason: the ownership
    rule is inherited rather than written a third time.

    Flow, and **the order is the contract**: `GetBaseCv`, then `GetJobPosting`, then the CV's
    status. So a posting that isn't the requester's answers 404 even on an unextracted CV, on `GET`
    as on `POST` (S-23 before S-24). Their errors propagate unchanged (`BaseCvNotFound`,
    `JobPostingNotFound`, `GuestSessionExpired`, `UserNotFound`). Any status but `EXTRACTED` is
    `BaseCvNotReadyForScoring`. Normalizing and hashing run under `asyncio.to_thread`.
    """

    def __init__(self, get_base_cv: GetBaseCv, get_job_posting: GetJobPosting) -> None:
        self._get_base_cv = get_base_cv
        self._get_job_posting = get_job_posting

    async def __call__(
        self, owner: Owner, base_cv_id: BaseCvId, job_posting_id: JobPostingId
    ) -> ScoringPair:
        raise NotImplementedError
