"""The `scoring_fit_score` table and the imperative mapping for `FitScore` (ADR-0007, ADR-0040).

`properties=` targets the private attributes `FitScore.from_assessment` writes, since each column would
collide with a read-only `@property` of the same short name. `_recorded_events` is absent: an in-memory
outbox, not a fact about the score. **Never `class FitScore(Base)`.**

**No `version_id_col`:** only a success is a row and nothing mutates it (FS-3). The claim's Core `UPDATE`
names the two owner columns and nothing else.

**Two plain unique constraints are enough:** `NULL`s are distinct and every row is a success, so no partial
index is needed (plan §0.3). Their names are explicit because the convention's would run past Postgres's 63
characters and be truncated. Each also serves its owner's FK cascade and `scored_at_since`.

The authoritative migration is `alembic/versions/…_add_scoring_tables.py`; `alembic check` proves this
`Table` matches it.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.registry import mapper_registry, metadata
from tailorcraft.infrastructure.persistence.types.identity import GuestSessionIdType, UserIdType
from tailorcraft.infrastructure.persistence.types.scoring import (
    CvFingerprintType,
    FitGapsType,
    FitScoreIdType,
    MicroUsdType,
    ModelNameType,
    PostingFingerprintType,
    PromptVersionType,
    ScoredCvRefType,
    ScoredPostingRefType,
    ScoredRunRefType,
    ScorePercentType,
)

fit_score_table = Table(
    "scoring_fit_score",
    metadata,
    # Application-assigned UUIDv7 (ADR-0007): no server default.
    Column("id", FitScoreIdType, primary_key=True),
    # The owner, ADR-0022's shape. Each FK's `ON DELETE CASCADE` is how the purge and account erasure
    # reach this table, and each FK refusing is how `store` learns the owner went during the call.
    Column(
        "guest_session_id",
        GuestSessionIdType,
        ForeignKey(guest_session_table.c.id, ondelete="CASCADE"),
        nullable=True,
    ),
    Column("user_id", UserIdType, ForeignKey(user_table.c.id, ondelete="CASCADE"), nullable=True),
    # **No foreign key on either** (ADR-0014's convention, ADR-0040 decision 8): they are what deletion
    # matches on, not the identity. The race an FK would close is closed by `store`'s two locks.
    Column("base_cv_id", ScoredCvRefType, nullable=False),
    Column("job_posting_id", ScoredPostingRefType, nullable=False),
    # The identity, per owner. Never logged, evented or returned.
    Column("cv_fingerprint", CvFingerprintType, nullable=False),
    Column("posting_fingerprint", PostingFingerprintType, nullable=False),
    Column("score", ScorePercentType, nullable=False),
    # PII: derived from a CV.
    Column("gaps", FitGapsType, nullable=False),
    Column("model", ModelNameType, nullable=False),
    Column("prompt_version", PromptVersionType, nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("unmetered_attempts", Integer, nullable=False),
    Column("llm_duration_ms", Integer, nullable=False),
    Column("cost_microusd", MicroUsdType, nullable=False),
    Column("scored_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    # Slice 5.2 (plan §0.8): the run whose draft CV was judged, `NULL` for a base CV. No foreign key
    # (ADR-0014); a history-entry deletion deletes the run's draft scores by this column.
    Column("tailoring_run_id", ScoredRunRefType, nullable=True),
    # FS-1.
    CheckConstraint("num_nonnulls(guest_session_id, user_id) = 1", name="exactly_one_owner"),
    CheckConstraint("score BETWEEN 0 AND 100", name="score_in_range"),
    UniqueConstraint(
        "guest_session_id",
        "cv_fingerprint",
        "posting_fingerprint",
        name="uq_scoring_fit_score_guest_pair",
    ),
    UniqueConstraint(
        "user_id",
        "cv_fingerprint",
        "posting_fingerprint",
        name="uq_scoring_fit_score_user_pair",
    ),
    # Partial: almost every row is a base CV's, and only a run's deletion reads by this column.
    Index(
        "ix_scoring_fit_score_tailoring_run_id",
        "tailoring_run_id",
        postgresql_where=text("tailoring_run_id IS NOT NULL"),
    ),
)

mapper_registry.map_imperatively(
    FitScore,
    fit_score_table,
    properties={
        "_id": fit_score_table.c.id,
        "_owner_guest_session_id": fit_score_table.c.guest_session_id,
        "_owner_user_id": fit_score_table.c.user_id,
        "_base_cv_id": fit_score_table.c.base_cv_id,
        "_job_posting_id": fit_score_table.c.job_posting_id,
        "_cv_fingerprint": fit_score_table.c.cv_fingerprint,
        "_posting_fingerprint": fit_score_table.c.posting_fingerprint,
        "_score": fit_score_table.c.score,
        "_gaps": fit_score_table.c.gaps,
        "_model": fit_score_table.c.model,
        "_prompt_version": fit_score_table.c.prompt_version,
        "_input_tokens": fit_score_table.c.input_tokens,
        "_output_tokens": fit_score_table.c.output_tokens,
        "_unmetered_attempts": fit_score_table.c.unmetered_attempts,
        "_llm_duration_ms": fit_score_table.c.llm_duration_ms,
        "_cost": fit_score_table.c.cost_microusd,
        "_scored_at": fit_score_table.c.scored_at,
        "_draft_of": fit_score_table.c.tailoring_run_id,
    },
)
