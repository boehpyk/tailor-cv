"""The `export_job` table and the imperative mapping for `ExportJob` (ADR-0007, ADR-0016).

As with `intake_base_cv`, `posting_job_posting` and `tailoring_run`, every column collides with a
read-only `@property` of the same short name on the aggregate (`id`, `status`, `format`, …), so the
`properties=` dict below targets the **private** attributes (`_id`, `_status`, `_format`, …) that
`request`, `mark_started`, `mark_ready` and `mark_failed` actually write. Renaming one of those
fifteen attributes in `domain/export/export_job.py` is a breaking change to this module, and the
aggregate carries a comment saying so.

`ExportJob` composes `RecordsEvents`, which gives every instance a `_recorded_events` buffer
(`domain/shared/events.py`). That attribute is deliberately **absent** from both the table and
`properties=`: it is not a persisted fact about the job, it is an in-memory outbox the use case
drains via `release_events()` before the transaction commits. Naming only the fifteen mapped
attributes below is what keeps SQLAlchemy from ever trying to instrument it.

**Never `class ExportJob(Base)`, never `mapped_column` on the domain class.** That is the tutorial
path and it ends the design: the aggregate would import SQLAlchemy and `domain/` would stop being
pure (ADR-0002).

**Fifteen columns, fifteen mapped attributes, zero assembling properties** — and after
`tailoring_run`'s sixteen-columns-to-two-composites asymmetry (OQ-5) that is worth one sentence
rather than none. `ExportJob` holds no multi-column value object at all. Its one derived value,
`storage_ref`, is a pure function of `_id` and `_format` (`FileRef.for_export`, XJ-7), so it is
computed rather than stored, and the `file_key` column exists only because 1.6 must be able to read
a key from a row *without* reconstructing the aggregate. The two are the same string by
construction, which is the guarantee AC-23 pins down.

**This table holds no PII** (ADR-0016 §4) — ids, enums, integers, instants and a storage key. No
name, no text, no path, no client IP. A `SELECT *`, a failed-`UPDATE` message or a `db.dump` of this
table alone discloses nothing about the person, and that is a property to defend the next time a
column is proposed here: the tempting one is a snapshot of the document being rendered, which
ADR-0016 §3 refused twice over.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, Integer, Table, text
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.registry import mapper_registry, metadata
from tailorcraft.infrastructure.persistence.types.export import (
    ExportFailureReasonType,
    ExportFormatType,
    ExportJobIdType,
    ExportJobStatusType,
)
from tailorcraft.infrastructure.persistence.types.identity import GuestSessionIdType
from tailorcraft.infrastructure.persistence.types.shared import FileRefType
from tailorcraft.infrastructure.persistence.types.tailoring import (
    TailoredDocumentKindType,
    TailoringRunIdType,
)

export_job_table = Table(
    # Not `export_export_job`: `tailoring_run` set the precedent for collapsing the repeated word
    # when an aggregate's name already starts with its context's (ADR-0007).
    "export_job",
    metadata,
    # Application-assigned UUIDv7 from `ExportJobRepository.next_identity()` (ADR-0007). No server
    # default and no sequence — the database is told the id, it never invents one. Here that does a
    # second job it does nowhere else in this schema: the id is the sole input to the job's storage
    # key (`FileRef.for_export`, XJ-7), so the *file's name is known before this row is committed*
    # and before a single byte is rendered.
    Column("id", ExportJobIdType, primary_key=True),
    # `ondelete="CASCADE"` is half of the retention contract 1.6 consumes (ADR-0016's amendment to
    # ADR-0011, AC-22): purging an expired `identity_guest_session` row deletes every export job it
    # owns in the same statement, so the purge still needs no predicate beyond
    # `identity_guest_session.expires_at < now()`. The other half is `file_key`, below.
    #
    # Indexed because this one column serves two readers: `count_for_session` (the `TooManyExportJobs`
    # cap) and the cascade itself. `registry.py`'s convention renders it
    # `ix_export_job_guest_session_id`.
    Column(
        "guest_session_id",
        GuestSessionIdType,
        ForeignKey(guest_session_table.c.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    # **No foreign key to `tailoring_run`, deliberately** — the same call `tailoring_run` made for
    # `base_cv_id` and `job_posting_id`, and the reasoning is recorded there in full: an aggregate
    # references another aggregate **by identity**, not by a database relationship, and both tables
    # already cascade from `identity_guest_session`, so an FK here would add a write-time check and
    # a second cascade path for a guarantee the session FK already gives.
    #
    # It still uses the typed decorator rather than a bare `postgresql.UUID`, so a loaded job hands
    # back a `TailoringRunId`. With three UUID columns in one table — and two of them arriving from
    # the *same URL* on the download path — the types are what stop a transposition from becoming a
    # lookup that quietly finds nothing.
    #
    # Indexed for two readers: `list_for_run` (the reattach-after-refresh endpoint) and
    # `find_latest_for_key` (the idempotency lookup, which filters `document` and `format` on top and
    # orders by `requested_at DESC, id DESC LIMIT 1`). A run holds a few dozen jobs at the very most,
    # so this index plus a filter and a sort is free. **A composite index on
    # `(tailoring_run_id, document, format, requested_at DESC)` is the change to make if that ever
    # stops being true** — named here so its absence reads as a decision rather than an oversight.
    Column("tailoring_run_id", TailoringRunIdType, nullable=False, index=True),
    # The discriminator: which of the run's two documents this job rendered. `TailoredDocumentKindType`
    # is imported from `types/tailoring.py` rather than declared in `types/export.py`, because the
    # enum is `tailoring`'s — the decorator's own docstring carries that argument and the reason 1.4
    # needed no such column.
    Column("document", TailoredDocumentKindType, nullable=False),
    # **The third lock on XJ-2** ("a job may not exist for an inline format"). The first is
    # `ExportJob.request`, the second is `FileRef.for_export`; this one is the only one that binds a
    # hand-written `INSERT`, which is the whole reason a constraint exists beside two Python guards.
    #
    # Written as a membership test over the two queued formats rather than as a negation of the two
    # inline ones. The difference shows up when a fifth format is added: `format NOT IN ('md','txt')`
    # would silently admit it, while this refuses it until someone decides, in this file, that it has
    # a file.
    Column("format", ExportFormatType, nullable=False),
    # ADR-0015's version of the run at request time — the number `was_requested_for` compares against
    # to decide `source_changed` (ADR-0016 (b)). Not a foreign key into anything and not the *job's*
    # version (that is `version`, at the bottom); the two integers are unrelated and sit apart in the
    # column order for that reason.
    Column("run_version", Integer, nullable=False),
    # `VARCHAR(16)` + `ExportJobStatusType`, **not** a native Postgres `ENUM` — the same call every
    # other status column in this schema makes, because adding a member to a native enum takes a
    # lock. The enforcement is the aggregate's twelve-cell transition table plus the CHECKs below.
    Column("status", ExportJobStatusType, nullable=False),
    Column("failure_reason", ExportFailureReasonType, nullable=True),
    # **The other half of the retention contract** (AC-23): `row.file_key == FileRef.for_export(
    # row.id, row.format).key` for every `ready` job, so 1.6 can read the keys of an expiring
    # session's jobs before the cascade *or* reconstruct them from the ids afterwards, and unlink
    # after the rows are gone (ADR-0006 §2's order).
    #
    # `UNIQUE` because the key is a pure function of the id, which is a primary key — so a duplicate
    # here is not a business rule being enforced, it is a *corruption detector*. The only way two
    # rows can hold one key is if something wrote a key it did not derive, and this constraint turns
    # that into a failed write instead of two jobs whose downloads silently serve one file. It costs
    # one index on a mostly-`NULL` column (PostgreSQL's btree skips `NULL`s for uniqueness, so every
    # `queued`, `rendering` and `failed` row is free of it).
    #
    # `FileRefType` is **reused** from `types/shared.py`, where 1.1 put it for exactly this moment —
    # one value object, one decorator, so the storage grammar cannot be re-validated by two different
    # rules on the way out of two tables. That makes the column `VARCHAR(512)` where the technical
    # plan's table says `VARCHAR(64)`; the decorator won, and the reasoning is on `FileRefType`.
    Column("file_key", FileRefType, nullable=True, unique=True),
    # Two integers written together with `file_key` by `mark_ready` and by nothing else (XJ-3). The
    # size is what the download handler answers `Content-Length` from without stat-ing the file.
    Column("byte_size", Integer, nullable=True),
    # **Milliseconds, on purpose, and meant to look inconsistent with the three whole-second
    # timestamps below** — the same deliberate inconsistency `tailoring_run.llm_duration_ms` carries,
    # commented here again because a reader who spots it should find the reason rather than "fix" it.
    # This is not a clock reading; it is a duration the adapter measures with `time.perf_counter()`.
    # A PDF render is budgeted in single-digit seconds, so whole-second resolution would erase the
    # entire signal it exists to carry.
    Column("render_duration_ms", Integer, nullable=True),
    # `TIMESTAMP(timezone=True, precision=0)` on all three: whole-second, per the `Clock` contract
    # (ADR-0007). The system clock truncates at the source, so a database round trip can never change
    # a value — which is what makes an equality assertion on a reloaded job safe rather than
    # microsecond-dependent, and what makes the `id` tiebreaks in the repository's two ordered reads
    # necessary rather than decorative.
    Column("requested_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    Column("started_at", TIMESTAMP(timezone=True, precision=0), nullable=True),
    Column("completed_at", TIMESTAMP(timezone=True, precision=0), nullable=True),
    # The optimistic-concurrency counter, declared as the mapper's `version_id_col` below with
    # `version_id_generator=False` (ADR-0015 §3, XJ-6). `server_default="1"` is **not** for the
    # application — `ExportJob.request` sets 1 explicitly and every transition bumps it — it is for
    # hand-written `INSERT`s. Unlike `tailoring_run.version` it has no two-version deploy window to
    # serve, because the table and the column ship in the same release; the default is kept anyway so
    # that the two `version` columns in this schema behave identically under `psql`.
    Column("version", Integer, nullable=False, server_default=text("1")),
    # ---------------------------------------------------------------------------------------------
    # **Seven CHECKs, one per column, and the "one per column" is the load-bearing part.**
    #
    # 1.3's AC-4 amendment was measured, not reasoned about: the tempting form compares a status
    # against a *conjunction* — `(status = 'ready') = (file_key IS NOT NULL AND byte_size IS NOT NULL
    # AND render_duration_ms IS NOT NULL)` — and a non-`ready` row holding exactly one of the three
    # satisfies it (`false = false`). Postgres would accept `UPDATE export_job SET byte_size = 1` on
    # a failed job. XJ-3 is "a ready job has all three; a partial one is not a state", and a half-row
    # is exactly what the conjunction admits. So each column gets its own equality.
    #
    # Mind `registry.py`'s naming convention — `"ck": "ck_%(table_name)s_%(constraint_name)s"` — so
    # each `name=` below is the `constraint_name` slot only, and these render as
    # `ck_export_job_format_is_queued` and friends.
    #
    # Each is an equality between two predicates rather than an implication, which is what makes one
    # expression reject **both** bad pairings: a ready job missing its size, and a queued job that
    # somehow has one.
    #
    # The aggregate already makes every one of these unrepresentable — four named transitions, no
    # setters, an empty `__init__` that refuses arguments. These seven bind what three methods
    # cannot: a hand-written `UPDATE`, a bad backfill, a `psql` session at 2 a.m. Two independent
    # mechanisms, which is what an invariant the download path trusts deserves.
    # ---------------------------------------------------------------------------------------------
    CheckConstraint(
        "format IN ('pdf','docx')",
        name="format_is_queued",
    ),
    # XJ-8: a run version is a counter that starts at 1, so 0 or a negative is a value no run ever
    # had. `ExportJob.request` raises `InvalidRunVersion` below 1 (AC-2); this is the same rule where
    # Python cannot reach. No upper bound — there is no number of edits that makes a job invalid.
    CheckConstraint(
        "run_version >= 1",
        name="run_version_positive",
    ),
    # XJ-3, three times, one column each. `mark_ready` writes all three together or the transition
    # does not happen.
    CheckConstraint(
        "(status = 'ready') = (file_key IS NOT NULL)",
        name="file_key_matches_status",
    ),
    CheckConstraint(
        "(status = 'ready') = (byte_size IS NOT NULL)",
        name="byte_size_matches_status",
    ),
    CheckConstraint(
        "(status = 'ready') = (render_duration_ms IS NOT NULL)",
        name="render_duration_matches_status",
    ),
    CheckConstraint(
        "(status = 'failed') = (failure_reason IS NOT NULL)",
        name="failure_reason_matches_status",
    ),
    # The two terminal statuses are exactly the two that carry a `completed_at`. **`started_at` gets
    # no such constraint, and the omission is deliberate** — the same one `tailoring_run` records:
    # `mark_failed` is legal from `queued`, so a `failed` job may legitimately have no `started_at`
    # at all. Two failures are recorded from `queued` here rather than 1.3's one: a refused enqueue
    # (`not_queued`, X-22) and a run whose documents changed under a job that never started
    # (`source_changed`). A constraint tying the two would forbid both.
    CheckConstraint(
        "(status IN ('ready','failed')) = (completed_at IS NOT NULL)",
        name="completed_at_matches_terminal_status",
    ),
    # **The partial index for the stale-render sweep (X-29)**, the twin of 1.3's
    # `ix_tailoring_run_running_started_at` and built on the same argument: `list_stale_rendering`
    # names no session and no run, so without this index it has only the table to scan — every minute,
    # for ever, against a table that keeps every export ever requested. Partial because the
    # `rendering` set is tiny whatever the table's size: a row is in it only while a worker holds the
    # render, so the index has a handful of entries on a busy day and none on a quiet one, and the
    # terminal rows that are nearly the whole table never enter it.
    #
    # **Two columns where 1.3's twin has one, and the contradiction is deliberate** (CLAUDE.md: say
    # why at the point of contradiction). The sweep's total order is `(started_at, id)`, so including
    # `id` keeps the tiebreak inside the index rather than sending the executor to the heap for it.
    # What it does **not** buy is a free sort: the query folds `NULL` `started_at` to the front
    # (`NULLS FIRST`) while a Postgres btree defaults to `NULLS LAST`, so the planner will still sort
    # the rows this index returns. That is accepted for 1.3's reason — the `rendering` set is a
    # handful of rows and sorting it is trivial — rather than fixed by declaring the index
    # `started_at NULLS FIRST`, which would be an expression index for no measurable gain and one
    # more line for autogenerate to render wrong.
    #
    # Named explicitly: left unnamed the `ix` convention would render it
    # `ix_export_job_started_at_id`, which reads as a full index on two columns. The predicate
    # belongs in the name.
    #
    # The predicate is a `text()` literal, as the CHECKs above are. **The query must state
    # `'rendering'` as a constant as well, or the planner may ignore this index** — a generic
    # prepared plan holding `status = $1` cannot prove the query's `WHERE` implies the index's. So
    # `list_stale_rendering` renders the status with `literal_execute=True`.
    Index(
        "ix_export_job_rendering_started_at",
        "started_at",
        "id",
        postgresql_where=text("status = 'rendering'"),
    ),
)

mapper_registry.map_imperatively(
    ExportJob,
    export_job_table,
    properties={
        "_id": export_job_table.c.id,
        "_guest_session_id": export_job_table.c.guest_session_id,
        "_tailoring_run_id": export_job_table.c.tailoring_run_id,
        "_document": export_job_table.c.document,
        "_format": export_job_table.c.format,
        "_run_version": export_job_table.c.run_version,
        "_status": export_job_table.c.status,
        "_failure_reason": export_job_table.c.failure_reason,
        "_file_key": export_job_table.c.file_key,
        "_byte_size": export_job_table.c.byte_size,
        "_render_duration_ms": export_job_table.c.render_duration_ms,
        "_requested_at": export_job_table.c.requested_at,
        "_started_at": export_job_table.c.started_at,
        "_completed_at": export_job_table.c.completed_at,
        "_version": export_job_table.c.version,
    },
    # **The optimistic-concurrency seam** (ADR-0015 §3), declared exactly as `tailoring_run`'s is,
    # and it is the mechanism behind AC-6 rather than a precaution copied for symmetry.
    #
    # `version_id_col` is what it buys: on every flush of a dirty job SQLAlchemy emits
    # `UPDATE export_job SET … WHERE id = :id AND version = :loaded` and raises `StaleDataError` when
    # zero rows match. The repository translates that into `ExportJobConcurrentlyModified`. That one
    # column is what makes **two simultaneous deliveries of one job render exactly once**: both read
    # `queued`, both pass `ExportAlreadyStarted` in memory, and the loser's `save` after
    # `mark_started` matches no row — so `RenderExportJob` returns `SKIPPED` **before** the worker
    # spends a second on WeasyPrint, rather than after.
    #
    # `version_id_generator=False` is what keeps the number **the domain's**: SQLAlchemy would
    # otherwise increment the column itself on every flush, making `version` a value the aggregate
    # never set, and every domain test of a transition would need a database to observe it. With the
    # generator off, `ExportJob` does the `+= 1` in each transition (XJ-6) and the mapper only
    # *checks*.
    #
    # And what that costs, which is the half worth remembering: with the generator off, the
    # `WHERE version = :loaded` only detects a race the aggregate bumped past. A transition that
    # forgets its `+= 1` writes the number it loaded, matches its own row, and has **no concurrency
    # protection at all** — silently. That is why "every transition increments" is XJ-6 with a
    # table-driven test over every legal path, and why each transition in
    # `domain/export/export_job.py` carries the same one-line comment.
    version_id_col=export_job_table.c.version,
    version_id_generator=False,
)
