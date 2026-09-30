"""The `posting_job_posting` table and the imperative mapping for `JobPosting` (ADR-0007).

As with `intake_base_cv`, every column collides with a read-only `@property` of the same short name
on `JobPosting` (`id`, `source`, `text`, …), so the `properties=` dict below targets the **private**
attributes (`_id`, `_source`, `_text`, …) that the two constructors actually write. `JobPosting` also
composes `RecordsEvents`, which gives every instance a `_recorded_events` buffer — that attribute is
deliberately **absent** from both the table and `properties=`: it is not a persisted fact about the
aggregate, it is an in-memory outbox that `CaptureJobPosting` drains via `release_events()` before
the transaction commits. Naming only the eight mapped attributes is what keeps SQLAlchemy from ever
trying to instrument it.

**Slice 2.3 (ADR-0023): one aggregate, two owner shapes, one table** — `intake_base_cv`'s 2.2 shape
(ADR-0022), copied rather than shared. The domain's owner is a sum type, `GuestOwner | UserOwner`;
a foreign key has one target table, so the table stores a product — `guest_session_id NULL`,
`user_id NULL` — and `ck_posting_job_posting_exactly_one_owner` restores "exactly one". The
translation happens in `JobPosting.owner` / `_assign_owner` alone; this module maps the two private
attributes those read and write.

**Never `class JobPosting(Base)`, never `mapped_column` on the domain class.** That is the tutorial
path and it ends the design: the aggregate would import SQLAlchemy and `domain/` would stop being
pure (ADR-0002).
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, Table, text
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.registry import mapper_registry, metadata
from tailorcraft.infrastructure.persistence.types.identity import GuestSessionIdType, UserIdType
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
    # `count_for_owner` for a guest, and the cascade itself. The naming convention in `registry.py`
    # renders this as `ix_posting_job_posting_guest_session_id`. It exists before 1.6 needs it, so
    # the first purge run is not also the first sequential scan of a growing table.
    #
    # **Nullable since 2.3** (ADR-0022's shape, ADR-0023), and that is what spares a signed-in
    # user's posting from the purge *by schema* rather than by a `WHERE`: a user-owned row has
    # `guest_session_id IS NULL`, so the cascade from `identity_guest_session` has no path to it and
    # the purge's `IN (…)` never matches it. "Exactly one owner" moved from this `NOT NULL` to
    # `ck_posting_job_posting_exactly_one_owner` below.
    Column(
        "guest_session_id",
        GuestSessionIdType,
        ForeignKey(guest_session_table.c.id, ondelete="CASCADE"),
        nullable=True,
        index=True,
    ),
    # The user half of the owner (2.3). `ON DELETE CASCADE` so erasing an account takes its
    # postings in the same statement as its saved CVs and runs (`SqlAlchemyAccountData
    # .delete_account`) — a posting holds no file, so nothing has to be collected first. A single
    # posting also goes when its history entry is deleted and no other run references it
    # (`SqlAlchemyHistoryEntryData`, technical plan §0.6).
    #
    # The FK's name, `fk_posting_job_posting_user_id_identity_user`, is load-bearing:
    # `SqlAlchemyJobPostingRepository.add` recognises a capture racing an account erasure by it and
    # answers `UserNotFound` (H-53). Indexed by the composite `ix_posting_job_posting_user_id_created_at`
    # below, whose leading column serves the cascade and every `user_id = :u` seek.
    Column(
        "user_id",
        UserIdType,
        ForeignKey(user_table.c.id, ondelete="CASCADE"),
        nullable=True,
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
    #
    # The second lock on J-1 ("exactly one owner"), behind `JobPosting._assign_owner`: the product of
    # two nullable owner columns has four states and the domain's sum type two. `num_nonnulls`
    # refuses both others — two owners, and none — and stays correct if a third owner column is ever
    # added to its argument list. `ck_posting_job_posting_exactly_one_owner`, 2.2's shape exactly.
    CheckConstraint("num_nonnulls(guest_session_id, user_id) = 1", name="exactly_one_owner"),
    # `list_recent_for_user` (`ORDER BY created_at DESC, id DESC LIMIT n`) reads this index in order
    # and stops; `count_for_owner` for a user, account erasure and the cascade from `identity_user`
    # all seek on its leading column. Composite and descending to match that sort exactly, so the
    # "recent postings" list is an index range scan rather than a sort over the user's postings.
    #
    # Named explicitly and with `text()` columns: the `ix` convention cannot name an expression
    # column. (Plan §5 expected autogenerate to mangle the `DESC`; measured against Alembic 1.19.1 it
    # rendered `literal_column('… DESC')` correctly — `03494836ce30`'s docstring has the review.)
    Index(
        "ix_posting_job_posting_user_id_created_at",
        "user_id",
        text("created_at DESC"),
        text("id DESC"),
    ),
)

mapper_registry.map_imperatively(
    JobPosting,
    job_posting_table,
    properties={
        "_id": job_posting_table.c.id,
        # The two halves of the owner (ADR-0022): two attributes, one fact. `JobPosting._assign_owner`
        # is their only writer and `JobPosting.owner` their only reader — see the class comment there.
        "_owner_guest_session_id": job_posting_table.c.guest_session_id,
        "_owner_user_id": job_posting_table.c.user_id,
        "_source": job_posting_table.c.source,
        "_source_url": job_posting_table.c.source_url,
        "_title": job_posting_table.c.title,
        "_text": job_posting_table.c.text,
        "_created_at": job_posting_table.c.created_at,
    },
)
