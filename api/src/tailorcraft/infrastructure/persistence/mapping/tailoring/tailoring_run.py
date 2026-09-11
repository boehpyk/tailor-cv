"""The `tailoring_run` table and the imperative mapping for `TailoringRun` (ADR-0007).

As with `intake_base_cv` and `posting_job_posting`, every column collides with a read-only
`@property` of the same short name on the aggregate (`id`, `status`, `failure_reason`, …), so the
`properties=` dict below targets the **private** attributes (`_id`, `_status`, `_failure_reason`, …)
that `request`, `mark_started`, `mark_succeeded` and `mark_failed` actually write. Renaming one of
those attributes in `domain/tailoring/tailoring_run.py` is a breaking change to this module, and the
aggregate carries a comment saying so.

`TailoringRun` composes `RecordsEvents`, which gives every instance a `_recorded_events` buffer
(`domain/shared/events.py`). That attribute is deliberately **absent** from both the table and
`properties=`: it is not a persisted fact about the run, it is an in-memory outbox that the use case
drains via `release_events()` before the transaction commits. Naming only the sixteen mapped
attributes below is what keeps SQLAlchemy from ever trying to instrument it.

**Never `class TailoringRun(Base)`, never `mapped_column` on the domain class.** That is the
tutorial path and it ends the design: the aggregate would import SQLAlchemy and `domain/` would stop
being pure (ADR-0002).

**The one asymmetry in this mapping, resolved here on purpose (OQ-5).** Sixteen mapped attributes,
**two assembling properties, zero composites.** Seven of the sixteen columns back two multi-column
value objects — `tailored_cv` + `cover_letter` make a `TailoredDocuments`, and the five provenance
and cost columns make an `LlmCallMetrics` — and SQLAlchemy's obvious answer for exactly that shape is
`composite()`. **ADR-0007 forbids it** ("value objects map through `TypeDecorator`s, not
composites"), so neither composite is a mapped attribute at all: the mapping targets the seven
private scalars, `TailoringRun.documents` and `TailoringRun.metrics` assemble the value objects on
read, and `mark_succeeded` writes through to the scalars. The domain still deals in two composite
value objects; only the storage is seven columns.

The reason is written at this seam because this is where a later reader meets the asymmetry: seven
columns, two properties, and an obvious-looking tidy-up into `composite()` that would drag a
SQLAlchemy construct into the domain's vocabulary and break the only rule ADR-0007 states about
value objects. The alternative that *was* considered — expose seven optional value objects on the
aggregate and drop the two composites — is recorded in `TailoringRun.documents`' docstring and costs
more than it saves: TR-5 becomes a two-part invariant and TR-7 a five-part one, each re-checked
rather than made unconstructable, and every read site turns into a multi-part narrowing where today
it is one `if run.documents is not None`.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, Table
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.registry import mapper_registry, metadata
from tailorcraft.infrastructure.persistence.types.identity import GuestSessionIdType
from tailorcraft.infrastructure.persistence.types.intake import BaseCvIdType
from tailorcraft.infrastructure.persistence.types.posting import JobPostingIdType
from tailorcraft.infrastructure.persistence.types.tailoring import (
    CoverLetterType,
    ModelNameType,
    PromptVersionType,
    TailoredCvType,
    TailoringFailureReasonType,
    TailoringRunIdType,
    TailoringRunStatusType,
)

tailoring_run_table = Table(
    "tailoring_run",
    metadata,
    # Application-assigned UUIDv7 from `TailoringRunRepository.next_identity()` (ADR-0007): the run
    # is a valid aggregate, with its `TailoringRunRequested` event already recorded, before it ever
    # meets the database. No server default and no sequence — the database is told the id, it never
    # invents one.
    Column("id", TailoringRunIdType, primary_key=True),
    # `ondelete="CASCADE"` is how this table joins ADR-0006's retention story without writing any
    # retention code: purging an expired `identity_guest_session` row deletes every run it owns in
    # the same statement, so the 1.6 purge still needs no predicate beyond
    # `identity_guest_session.expires_at < now()`.
    #
    # Indexed because this one column serves four readers: the `GET /api/tailoring-runs` list query,
    # `count_for_session`, `find_active_for_session`, and the cascade itself. The naming convention
    # in `registry.py` renders it as `ix_tailoring_run_guest_session_id`. It exists before the purge
    # needs it, so the first purge run is not also the first sequential scan of a growing table.
    #
    # **No partial index on the active statuses**, and the absence is a decision rather than an
    # oversight: `find_active_for_session` filters `guest_session_id = ? AND status IN ('queued',
    # 'running')`, and with at most twenty runs per session this index plus a filter is free. A
    # partial index is the change to make if a session ever holds thousands of runs.
    Column(
        "guest_session_id",
        GuestSessionIdType,
        ForeignKey(guest_session_table.c.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    # **No foreign key on either of the next two columns, deliberately.** A reader who has just seen
    # `guest_session_id` carry one is owed the reason, and there are two.
    #
    # First: an aggregate references another aggregate **by identity**, not by a database
    # relationship. `intake` and `posting` are separate bounded contexts; a cross-context FK fuses
    # their tables into one schema and quietly forbids them from having independent lifecycles
    # later.
    #
    # Second, and this is the load-bearing one: **it would buy nothing.** All three tables already
    # cascade from `identity_guest_session`, so a run cannot outlive its inputs in practice — the
    # session's deletion takes all three in one statement. An FK here would add a write-time check
    # and a second cascade path for a guarantee the session FK already gives.
    #
    # The alternative (add both FKs for referential integrity) is recorded as considered. The
    # trigger to revisit it: if Phase 2 ever lets a user delete a single base CV while keeping the
    # session. The answer then is a **nullable reference plus a "the source CV was deleted" state**,
    # not a cascade that silently erases the history of what was produced and what it cost.
    #
    # They still use the typed decorators rather than a bare `postgresql.UUID`, so a loaded run
    # hands back a `BaseCvId` and a `JobPostingId` — with four UUID columns in one table, the types
    # are what stop a transposition from becoming a run against the wrong person's CV.
    Column("base_cv_id", BaseCvIdType, nullable=False),
    Column("job_posting_id", JobPostingIdType, nullable=False),
    # `VARCHAR(16)` + `TailoringRunStatusType`, **not** a native Postgres `ENUM` — the same call
    # `intake_base_cv.status` and `posting_job_posting.source` make, for the same reason: adding a
    # member to a native enum takes a lock. The real enforcement is the aggregate's transition table
    # plus the three CHECK constraints below.
    Column("status", TailoringRunStatusType, nullable=False),
    Column("failure_reason", TailoringFailureReasonType, nullable=True),
    # **Both document columns are PII** (Constitution §8): a tailored CV is a person's employment
    # history rewritten and a cover letter names where they want to work. Nothing logs either — the
    # events carry `cv_character_count` and `cover_letter_character_count` precisely so they need not
    # carry the documents (AC-22).
    #
    # Two named `TEXT` columns rather than one `JSONB` blob because they are two named things with
    # two different rules, not a schemaless bag. (No `JSONB` column appears in this slice at all;
    # when the first one does, it is `JSONB` and never `JSON`.)
    Column("tailored_cv", TailoredCvType, nullable=True),
    Column("cover_letter", CoverLetterType, nullable=True),
    # The five metric columns — the storage side of the OQ-5 decision in the module docstring.
    # `LlmCallMetrics` is not a mapped attribute; these five private scalars are, and
    # `TailoringRun.metrics` assembles them. All five are `NULL` together on every path but a
    # succeeded run: on most failures there is no successful call to describe, and a partially
    # filled metrics row would be worse than an absent one — it would show up in a token-spend total
    # as a real number that nothing was paid for.
    Column("model_name", ModelNameType, nullable=True),
    Column("prompt_version", PromptVersionType, nullable=True),
    Column("prompt_tokens", Integer, nullable=True),
    Column("completion_tokens", Integer, nullable=True),
    # **Milliseconds, on purpose, and it is meant to look inconsistent with the three whole-second
    # timestamps below.** Those are whole-second because the `Clock` port is whole-second by
    # contract; this is not a clock reading at all, it is a duration measured by the adapter with
    # `time.perf_counter()`. Constitution §7 sets a 15-second budget for a tailoring call, and a
    # whole-second resolution cannot defend it: the difference between 11 s and 14.4 s is the whole
    # signal, and rounded to seconds it is invisible. Do not "make it consistent" — the inconsistency
    # is the measurement working.
    Column("llm_duration_ms", Integer, nullable=True),
    # `TIMESTAMP(timezone=True, precision=0)` on all three: whole-second, per the `Clock` contract
    # (ADR-0007). The system clock truncates at the source, so a database round trip can never change
    # a value — which is what makes an equality assertion on a reloaded run safe rather than
    # microsecond-dependent.
    Column("requested_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    Column("started_at", TIMESTAMP(timezone=True, precision=0), nullable=True),
    Column("completed_at", TIMESTAMP(timezone=True, precision=0), nullable=True),
    # The three constraints below are TR-2 and TR-5 expressed where Python cannot reach. The
    # aggregate already makes every bad pairing unrepresentable — three named transitions, one
    # `TailoredDocuments` that requires both fields, no setters — but three methods do not bind a
    # hand-written `UPDATE`, a bad backfill, or a `psql` session at 2 a.m. Two independent
    # mechanisms, which is what an invariant this product's correctness rests on deserves.
    #
    # Mind `registry.py`'s naming convention — `"ck": "ck_%(table_name)s_%(constraint_name)s"` — so
    # each `name=` below is the `constraint_name` slot only, and these render as
    # `ck_tailoring_run_documents_match_status` and friends.
    #
    # Each is written as an equality between two predicates rather than as an implication, which is
    # what makes it reject **both** bad pairings in one expression: a succeeded run missing a
    # document, and a non-succeeded run that somehow holds one. Those are AC-4's four bad pairings,
    # two per constraint, and all four were verified rejected against a real Postgres.
    #
    # **The first one is written once per document column, not against a conjunction — and that is a
    # deliberate strengthening of the expression the spec originally carried.** The tempting form,
    # `(status = 'succeeded') = (tailored_cv IS NOT NULL AND cover_letter IS NOT NULL)`, compares a
    # status against a *conjunction*, so a NON-succeeded row holding exactly ONE of the two documents
    # satisfies it (`false = false`) and Postgres accepts
    # `UPDATE tailoring_run SET cover_letter = 'x'` on a failed run. That was measured against a real
    # database during T19, not reasoned about.
    #
    # It was strengthened rather than documented as a known gap, because the weaker form contradicts
    # the invariant this constraint exists to hold. **TR-5 is "a succeeded run has both documents;
    # half a result is a failure"** — and half a result in a row is exactly what the conjunction
    # admits. The constraint's whole justification (AC-4) is that *a bad backfill is not bound by
    # Python*, and a hand-written `UPDATE` is precisely how that state would arise: the aggregate
    # writes both documents together in `mark_succeeded` or neither, so no code path can reach it.
    # A guard whose only purpose is to stop hand-written SQL should not leave open the one shape of
    # hand-written SQL that breaks its invariant.
    #
    # The spec and the technical plan were amended in the same commit, so the migration (T21) and the
    # persistence tests (T33) are written against this form and not the old one. CLAUDE.md's rule:
    # when an acceptance criterion and the implementation disagree, fix one of them **on purpose**
    # and say which won. The criterion won; the expression changed.
    CheckConstraint(
        "(status = 'succeeded') = (tailored_cv IS NOT NULL)"
        " AND (status = 'succeeded') = (cover_letter IS NOT NULL)",
        name="documents_match_status",
    ),
    CheckConstraint(
        "(status = 'failed') = (failure_reason IS NOT NULL)",
        name="failure_reason_matches_status",
    ),
    # The two terminal statuses are exactly the two that carry a `completed_at`, so a run that is
    # still `queued` or `running` cannot have been completed and a decided one cannot lack the
    # moment it was decided. `started_at` gets **no** such constraint, and the omission is
    # deliberate: `mark_failed` is legal from `queued` (a refused enqueue, G-14; a stale redelivery,
    # G-25), so a `failed` run may legitimately have no `started_at` at all — a constraint tying the
    # two would forbid the very state the aggregate documents at length.
    CheckConstraint(
        "(status IN ('succeeded','failed')) = (completed_at IS NOT NULL)",
        name="completed_at_matches_terminal_status",
    ),
)

mapper_registry.map_imperatively(
    TailoringRun,
    tailoring_run_table,
    properties={
        "_id": tailoring_run_table.c.id,
        "_guest_session_id": tailoring_run_table.c.guest_session_id,
        "_base_cv_id": tailoring_run_table.c.base_cv_id,
        "_job_posting_id": tailoring_run_table.c.job_posting_id,
        "_status": tailoring_run_table.c.status,
        "_failure_reason": tailoring_run_table.c.failure_reason,
        # The seven scalars behind the two assembling properties (OQ-5, module docstring). Not a
        # `composite()`, not two mapped value objects — seven columns, seven private attributes.
        "_tailored_cv": tailoring_run_table.c.tailored_cv,
        "_cover_letter": tailoring_run_table.c.cover_letter,
        "_model_name": tailoring_run_table.c.model_name,
        "_prompt_version": tailoring_run_table.c.prompt_version,
        "_prompt_tokens": tailoring_run_table.c.prompt_tokens,
        "_completion_tokens": tailoring_run_table.c.completion_tokens,
        "_llm_duration_ms": tailoring_run_table.c.llm_duration_ms,
        "_requested_at": tailoring_run_table.c.requested_at,
        "_started_at": tailoring_run_table.c.started_at,
        "_completed_at": tailoring_run_table.c.completed_at,
    },
)
