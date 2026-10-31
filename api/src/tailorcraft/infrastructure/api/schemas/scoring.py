"""Request and response schemas for the `scoring` bounded context (slice 5.1, technical plan §4).

**What is absent is the contract:** no fingerprint, token count, cost or model name ever leaves in a
response, and no model or budget can be asked for in a request.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateFitScoreRequest(BaseModel):
    """`{"base_cv_id": "...", "job_posting_id": "..."}`. A malformed id or a stray field is a 422.
    Whether they exist and are the caller's is answered in the use case, every time."""

    model_config = ConfigDict(extra="forbid")

    base_cv_id: UUID
    job_posting_id: UUID


class FitScoreResponse(BaseModel):
    """One stored score. `expires_at` is the guest session's expiry, `null` for an account."""

    id: UUID
    score_percent: int = Field(ge=0, le=100)
    gaps: list[str] = Field(max_length=3)
    scored_at: datetime
    expires_at: datetime | None


class FitScoreForPairResponse(BaseModel):
    """`GET`'s answer. `null` is the ordinary "not scored yet", not an error."""

    fit_score: FitScoreResponse | None
