"""The `posting_job_posting` table and the imperative mapping for `JobPosting` (ADR-0007).

As with `intake_base_cv`, every column collides with a read-only `@property` of the same short name
on `JobPosting` (`id`, `source`, `text`, …), so the `properties=` dict below targets the **private**
attributes (`_id`, `_source`, `_text`, …) that the two constructors actually write. `JobPosting` also
composes `RecordsEvents`, which gives every instance a `_recorded_events` buffer — that attribute is
deliberately **absent** from both the table and `properties=`: it is not a persisted fact about the
aggregate, it is an in-memory outbox that `CaptureJobPosting` drains via `release_events()` before
the transaction commits. Naming only the seven mapped attributes is what keeps SQLAlchemy from ever
trying to instrument it.

**Never `class JobPosting(Base)`, never `mapped_column` on the domain class.** That is the tutorial
path and it ends the design: the aggregate would import SQLAlchemy and `domain/` would stop being
pure (ADR-0002).
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, ForeignKey, Table
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.registry import mapper_registry, metadata
from tailorcraft.infrastructure.persistence.types.identity import GuestSessionIdType
from tailorcraft.infrastructure.persistence.types.posting import (
    JobPostingIdType,
    JobPostingTextType,
    PostingSourceType,
    PostingTitleType,
    SourceUrlType,
)

job_posting_table = Table(
    "posting_job_posting",
    metadata,
    Column("id", JobPostingIdType, primary_key=True),
    # `ondelete="CASCADE"` is how this slice earns its place in ADR-0006's retention story without
    # writing any retention code: purging an expired `identity_guest_session` row deletes every
    # posting it owns in the same statement. The 1.6 purge therefore needs NO new predicate — it
    # stays `identity_guest_session.expires_at < now()` and nothing else. This slice adds a table,
    # not a rule.
    #
    # Indexed because the same column serves three readers: the `GET /api/job-postings` list query,
    # `count_for_session`, and the cascade itself. The naming convention in `registry.py` renders
    # this as `ix_posting_job_posting_guest_session_id`. It exists before 1.6 needs it, so the first
    # purge run is not also the first sequential scan of a growing table.
    Column(
        "guest_session_id",
        GuestSessionIdType,
        ForeignKey(guest_session_table.c.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("source", PostingSourceType, nullable=False),
    # Nullable, and the CHECK below is what ties it to `source`. **PII** (Constitution §8): this
    # column names a specific job at a specific company that a specific person is applying to.
    Column("source_url", SourceUrlType, nullable=True),
    Column("title", PostingTitleType, nullable=True),
    Column("text", JobPostingTextType, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    # Invariant J-2 expressed where Python cannot reach. The aggregate already makes the bad pairing
    # unconstructable — there are two named constructors and no third way — but two classmethods do
    # not bind a hand-written `UPDATE`, a bad backfill, or a migration that populates the table from
    # somewhere else. `(source = 'fetched') = (source_url IS NOT NULL)` reads as "these two facts
    # agree", and it rejects BOTH bad pairings: a fetched row with no URL and a pasted row with one.
    #
    # Mind `registry.py`'s naming convention — `"ck": "ck_%(table_name)s_%(constraint_name)s"` — so
    # the `name=` given here is the constraint_name slot and this becomes
    # `ck_posting_job_posting_source_url_matches_source`.
    CheckConstraint(
        "(source = 'fetched') = (source_url IS NOT NULL)",
        name="source_url_matches_source",
    ),
    # NO unique constraint on `source_url`, deliberately. A reader will ask why `intake_base_cv
    # .file_key` is unique and this is not: a `FileRef` is the address of a physical file we own, so
    # two rows pointing at one key makes "which row owns this file" unanswerable. A URL is an
    # *input*, not the address of anything we hold — two guests may legitimately capture the same
    # posting, and one guest may capture it twice after the page changed. Not deduplicated (P-37).
)

mapper_registry.map_imperatively(
    JobPosting,
    job_posting_table,
    properties={
        "_id": job_posting_table.c.id,
        "_guest_session_id": job_posting_table.c.guest_session_id,
        "_source": job_posting_table.c.source,
        "_source_url": job_posting_table.c.source_url,
        "_title": job_posting_table.c.title,
        "_text": job_posting_table.c.text,
        "_created_at": job_posting_table.c.created_at,
    },
)
