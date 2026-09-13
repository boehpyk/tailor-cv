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
drains via `release_events()` before the transaction commits. Naming only the twenty-one mapped
attributes below (sixteen from 1.3, five from 1.4's revision and version — ADR-0015) is what keeps
SQLAlchemy from ever trying to instrument it.

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

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, Integer, Table, text
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
    # Not `tailoring_tailoring_run`: AC-27 names this table, and the repeated word adds nothing.
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
    # Slice 1.4 (ADR-0015): the optimistic-concurrency counter and the user's revisions.
    #
    # `version` is declared as the mapper's `version_id_col` below (TR-8). `server_default="1"` is
    # **not** for the application — `TailoringRun.request` sets 1 explicitly and every transition
    # bumps it — it is for the two-version deploy window, where the *previous* application version
    # still inserts rows that know nothing of this column, and for hand-written `INSERT`s. Postgres
    # 11+ adds a constant default without a table rewrite, so the `ALTER` is instant on a live table.
    # The default stays after the deploy; there is no contract step.
    Column("version", Integer, nullable=False, server_default=text("1")),
    # The user's revision of each document, beside — never over — the model's draft in
    # `tailored_cv` / `cover_letter` (TR-11). **Both are PII** exactly as the two draft columns are.
    # The *same* `TypeDecorator`s are reused, no new one is added, which is what guarantees the draft
    # and the revision can never be validated by different rules (ADR-0015 §2).
    Column("edited_cv", TailoredCvType, nullable=True),
    Column("edited_cover_letter", CoverLetterType, nullable=True),
    # Whole-second `TIMESTAMP(timezone=True, precision=0)`, declared exactly as the three instants
    # above, for the same `Clock`-contract reason. Each is set together with its revision or not at
    # all (TR-10); the pairing CHECKs below hold that where Python cannot reach.
    Column("cv_edited_at", TIMESTAMP(timezone=True, precision=0), nullable=True),
    Column("cover_letter_edited_at", TIMESTAMP(timezone=True, precision=0), nullable=True),
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
    # deliberate: `mark_failed` is legal from `queued`, so a `failed` run may legitimately have no
    # `started_at` at all, and a constraint tying the two would forbid that state. Exactly one
    # failure is recorded from `queued`: a refused enqueue, `not_queued` (G-14), before any worker has
    # seen the run. `abandoned` (G-25') is not a second example, because it is recorded from
    # `running`, and a running run already has its `started_at`.
    CheckConstraint(
        "(status IN ('succeeded','failed')) = (completed_at IS NOT NULL)",
        name="completed_at_matches_terminal_status",
    ),
    # Slice 1.4's three CHECKs — TR-10 and TR-9 where Python cannot reach, declared in the same
    # shape as 1.3's three above (the `name=` is the `constraint_name` slot only; the convention in
    # `registry.py` renders them `ck_tailoring_run_cv_revision_pairs` and friends). 1.3's three are
    # untouched: they speak about the draft columns, which this slice never writes, and that is the
    # concrete payoff of an *additive* revision.
    #
    # The two pairing constraints are, like `documents_match_status`, an equality between two
    # predicates rather than an implication, so each rejects **both** bad pairings in one
    # expression: a revision without its instant, and an instant without its revision. `revise_cv`
    # and `revise_cover_letter` write the pair in one statement (TR-10); a hand-written `UPDATE`
    # need not, and this is what binds it.
    CheckConstraint(
        "(edited_cv IS NULL) = (cv_edited_at IS NULL)",
        name="cv_revision_pairs",
    ),
    CheckConstraint(
        "(edited_cover_letter IS NULL) = (cover_letter_edited_at IS NULL)",
        name="cover_letter_revision_pairs",
    ),
    # TR-9: a revision is legal only from `succeeded`. Written as an implication and not as an
    # equality on purpose — a succeeded run with **no** revision is the normal state of every run
    # the user never edited, so `(status = 'succeeded') = (edited_cv IS NOT NULL)` would be wrong.
    # `_guard_revisable` is the first line; this is the one that binds a `psql` session.
    CheckConstraint(
        "(edited_cv IS NULL AND edited_cover_letter IS NULL) OR status = 'succeeded'",
        name="revision_requires_success",
    ),
    # **A partial index for the stale-run sweep (G-25'), and it contradicts the "no partial index"
    # note on `guest_session_id` above on purpose.** The two queries differ in what bounds them.
    # `find_active_for_session` is bounded by its session: twenty runs at most, found through
    # `ix_tailoring_run_guest_session_id`, then filtered. `list_stale_running` names no session.
    # Without this index it has only the table to scan, and it runs **every minute, forever**,
    # against a table that keeps every run ever made, document bodies included.
    #
    # Partial because the `running` set is tiny whatever the table's size. A row is only in it
    # while a worker holds a call, so the index has a handful of entries on a busy day and zero on a
    # quiet one. The terminal rows, which are nearly the whole table, are never in it and cost it
    # nothing to write.
    #
    # On `started_at` alone, in the default order. It does **not** match the query's
    # `ORDER BY started_at NULLS FIRST, id`, and that is not an oversight. The index serves the
    # `WHERE`, and sorting the handful of rows it returns is trivial. Matching the sort would
    # buy nothing but a wider index.
    #
    # Named explicitly. Left unnamed, the `ix` convention in `registry.py` would render it
    # `ix_tailoring_run_started_at`, which reads as a full index on the column. The predicate belongs
    # in the name.
    #
    # The predicate is a `text()` literal, as the CHECKs above are. **The query must state
    # `'running'` as a constant as well, or the planner may ignore this index.** A partial index is
    # usable only when the planner can prove the query's `WHERE` implies the index's, and a generic
    # prepared plan holding `status = $1` proves nothing. So `list_stale_running` renders the status
    # with `literal_execute=True` rather than as a bound parameter.
    Index(
        "ix_tailoring_run_running_started_at",
        "started_at",
        postgresql_where=text("status = 'running'"),
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
        # Slice 1.4 (ADR-0015): the counter and the four revision scalars. `_edited_cv` and
        # `_edited_cover_letter` are two more nullable scalars behind an assembling property
        # (`TailoringRun.current_documents`), for the same OQ-5 reason as the seven above.
        "_version": tailoring_run_table.c.version,
        "_edited_cv": tailoring_run_table.c.edited_cv,
        "_edited_cover_letter": tailoring_run_table.c.edited_cover_letter,
        "_cv_edited_at": tailoring_run_table.c.cv_edited_at,
        "_cover_letter_edited_at": tailoring_run_table.c.cover_letter_edited_at,
    },
    # **The optimistic-concurrency seam (ADR-0015 §3), and what each half of it buys and costs.**
    #
    # `version_id_col` is what it *buys*: on every flush of a dirty run SQLAlchemy emits
    # `UPDATE tailoring_run SET … WHERE id = :id AND version = :loaded` and raises `StaleDataError`
    # when zero rows match — that is, when another process committed a newer version between this
    # one's load and its flush. The repository translates that into the domain's
    # `TailoringRunConcurrentlyModified`. One column closes two races: the stale edit (a `PUT` whose
    # `expected_version` matched in memory but lost at the row) and ADR-0014 amendment §6's
    # duplicate delivery (two in-flight deliveries both read `queued`; the second `save` matches no
    # row and `ExecuteTailoringRun` returns `SKIPPED` **before** paying).
    #
    # `version_id_generator=False` is what keeps the number **the domain's**: SQLAlchemy would
    # otherwise increment the column itself on every flush, making `version` a value the aggregate
    # never set — the edit contract would then be checked in the use case against a number the
    # domain could not see, and every domain test of `revise_*` would need a database. With the
    # generator off, `TailoringRun` does the `+= 1` in each transition (TR-8) and the mapper only
    # *checks*.
    #
    # And what that *costs*: with the generator off, the `WHERE version = :loaded` only detects a
    # race that the aggregate bumped past. A transition that forgets its `+= 1` writes
    # `WHERE version = :loaded` with the same number it loaded, matches its own row, and has **no
    # concurrency protection at all** — silently. That is why "every transition increments" is
    # invariant TR-8 with a table-driven test over every legal path, rather than a convention, and
    # why each transition in `domain/tailoring/tailoring_run.py` carries the same one-line comment.
    version_id_col=tailoring_run_table.c.version,
    version_id_generator=False,
)
