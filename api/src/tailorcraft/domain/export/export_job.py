"""The `ExportJob` aggregate: one request to render one document of one run into one file.

Composes `RecordsEvents` (`domain/shared/events.py`) rather than inheriting a shared aggregate base
class, and shares **no** base class with `TailoringRun`, `BaseCv` or `JobPosting` — CLAUDE.md is
explicit that two aggregates with the same shape do not get one. The shape is similar for the
fourth time (an id, an owner session, a requested-at, a status, a failure reason, a version) and the
similarity is the trap again. The rule that differs is on the face of this one: **a job may not
exist for an inline format** (XJ-2), and no other aggregate in this codebase has a constructor that
refuses one of its own enum's members. A supertype would either not hold that rule or would impose
it on three classes it makes no sense for.

This module was written in two steps (docs/sdlc.md §2): a skeleton of real signatures with
`NotImplementedError` bodies, so that `qa`'s tests failed on their *assertions* rather than on an
`ImportError`, and then the GREEN step that filled in `request`, the three transitions and the reads
against those recorded reds. Nothing in the tests was touched to get there.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tailorcraft.domain.export.errors import (
    ExportAlreadyDecided,
    ExportAlreadyStarted,
    ExportFormatNotQueued,
    ExportNotRendering,
    InvalidRunVersion,
)
from tailorcraft.domain.export.events import (
    ExportFailed,
    ExportReady,
    ExportRequested,
    ExportStarted,
)
from tailorcraft.domain.export.value_objects import (
    ExportDelivery,
    ExportFailureReason,
    ExportFormat,
    ExportJobId,
    ExportJobStatus,
)
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.domain.shared.events import RecordsEvents
from tailorcraft.domain.shared.files import FileRef

# `TailoredDocumentKind` and `TailoringRunId` are **imported from `domain/tailoring`, never
# redefined here.** A document kind is the *address* of a document on a run (ADR-0015 §1), and this
# context addresses the same two documents; a run id is the run's identity, and a job points at one.
# Copying either into `export` would give the codebase two enums with the same members whose values
# would be free to drift apart — and the string values are URL path segments and log fields, so the
# drift would show up as a mapping table nobody wrote.
#
# This is a domain-to-domain import **across bounded contexts**, which the layers contract permits
# (both sides are `domain`, so `lint-imports` and `test_domain_purity` have nothing to say about
# it). It is the ordinary relationship of a **downstream context to an upstream one's published
# language**, and it is strictly **one-directional**: `tailoring` never imports `export`, and the
# day it needs to is the day this points the wrong way and the shared terms belong in `shared`.
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId


# NOT `slots=True`: this aggregate is later mapped by SQLAlchemy's *imperative* mapping
# (`registry.map_imperatively`), which instruments attributes on the instance `__dict__` — a
# `__slots__` class has none, so mapping would fail at wiring time rather than at import time, which
# is a much worse place to discover it. This is a deliberate departure from the value objects one
# module over, which are all `slots=True` because nothing ever maps them directly: a reader who has
# just written `slots=True` on `ExportJobId` and arrives here should find the reason rather than
# "fix" the inconsistency. `TailoringRun`, `BaseCv` and `JobPosting` carry the same comment for the
# same reason, and the duplication is on purpose — a reader lands on one class, not on all four.
# The private-attribute names below are exactly what
# `infrastructure/persistence/mapping/export/export_job.py` will target, so renaming one here is a
# breaking change to that module too (ADR-0007).
class ExportJob(RecordsEvents):
    """One request to render one of a run's current documents, in one queued format, from one run
    version: who asked, for what, where it stands, and — once ready — the key of its file.

    **The legal-transition table.** This is the aggregate's whole behaviour, and AC-3 tests all
    twelve cells:

    | From \\ call | `mark_started` | `mark_ready` | `mark_failed` |
    |---|---|---|---|
    | `queued` | → `rendering` | `ExportNotRendering` | → `failed` |
    | `rendering` | `ExportAlreadyStarted` | → `ready` | → `failed` |
    | `ready` | `ExportAlreadyDecided` | `ExportAlreadyDecided` | `ExportAlreadyDecided` |
    | `failed` | `ExportAlreadyDecided` | `ExportAlreadyDecided` | `ExportAlreadyDecided` |

    **The one cell a reader will question is `mark_failed` from `queued`, and it is legal for
    exactly one reason**, as in 1.3's amendment: a failed enqueue (`not_queued`, X-22). The row
    commits and *then* the task is published, so a broker that refuses the publish leaves a
    committed job that can never render — the router records it `failed`/`not_queued` rather than
    leaving it `queued` for ever while a client polls it until it gives up. That failure may not be
    recorded by first pretending the job started: `started_at` means *a worker began a render*, and
    one invented to satisfy a state machine is a timestamp that lies to every latency measurement
    built on it.

    `abandoned` is **not** a second example of that cell. It is recorded from `rendering`, on a job
    whose `started_at` is real, once `is_stale` says no worker can still be on it — by a redelivered
    task past the stale window, or by the beat sweep that exists because redelivery alone does not
    bring a lost job back (X-29).

    Invariants (technical-plan.md):

    - **XJ-1** — A job always has exactly one owner session, one run id, one document kind and one
      format. `request` requires all four; there is no other constructor and no setter. The ids are
      typed rather than bare `UUID`s precisely because this aggregate holds *three* foreign ones
      plus its own, and transposing two would produce a job rendering the wrong person's document
      with no error anywhere.
    - **XJ-2** — **A job exists only for a queued format.** `request` with `md` or `txt` raises
      `ExportFormatNotQueued`. Mirrored as `CHECK (format IN ('pdf','docx'))`, and one lock further
      out as the `POST` body's `QueuedExportFormat` literal. Three locks on one rule, because the
      failure they prevent is a `to_thread` around WeasyPrint in a route: it passes every test with
      one user and collapses at five, with no error and nothing logged (CLAUDE.md, "Async is
      load-bearing, and violating it is silent").
    - **XJ-3** — `status == READY` **iff** `file_key`, `byte_size` and `render_duration_ms` are all
      set; `status == FAILED` **iff** `failure_reason` is set. Never both, never neither.
      `mark_ready` and `mark_failed` are the only writers and the table forbids a second decision.
      Mirrored as **four database `CHECK`s written once per column** (AC-4) — 1.3's lesson that a
      constraint compared against a conjunction happily accepts a half-row.
    - **XJ-4** — The outcome is decided **once**. The table's bottom two rows and
      `ExportAlreadyDecided`. **This is what makes the Celery task idempotent under
      `task_acks_late`** (AC-18): a redelivered task calls `mark_started` on a job that is no longer
      `queued` and is refused, so nothing is rendered twice and a redelivery arriving after an
      outcome cannot overwrite it. Idempotency here is a **domain** property, not a flag in a task.
    - **XJ-5** — `started_at >= requested_at`, and `completed_at >= started_at` when both are set.
      Guarded in each transition; raises `InvariantViolated` (`domain/shared/errors.py`). Time that
      runs backwards inside one row is not a rounding problem; it is a duration that comes out
      negative in a percentile three weeks later, and the aggregate is the cheapest place to refuse
      it.
    - **XJ-6** — `version` is 1 at request and **every** transition increments it by exactly one;
      nothing else writes it. **This is the invariant the concurrency control rests on** (ADR-0015
      §3): the mapping declares the column as `version_id_col` with `version_id_generator=False`, so
      SQLAlchemy's `WHERE version = :loaded` only *detects* a race the application bumped past — a
      transition that forgets to bump has **no** concurrency protection at all, and the symptom is
      two workers rendering one job. That is why a table-driven test walks every legal transition
      (AC-6) rather than trusting a convention.
    - **XJ-7** — `file_key` is always `FileRef.for_export(id, format)`. `mark_ready` computes it and
      there is no parameter through which a different one could be passed; `UNIQUE` on the column is
      the database's half. This is the retention hook 1.6 consumes (ADR-0016's amendment to
      ADR-0011).
    - **XJ-8** — `run_version >= 1`. `request` refuses otherwise with `InvalidRunVersion`; a run
      starts at 1 (ADR-0015) and only counts up, so anything below it is a caller that never read
      the run.
    - **XJ-9** — The key fields (session, run, document, format, run version) and `requested_at` are
      **immutable after creation**. Their absence from every transition, plus this row. A job is
      never "re-pointed" at a new version: that is a new job, which is what makes `run_version` mean
      something and what "Export again" does.

    **Deliberately not invariants of `ExportJob`:**

    - The per-session job cap and the "one current job per key" rule. Both span every job a session
      owns — facts no single instance can see — so they live in `RequestExport` with a comment
      saying why, exactly as `TooManyBaseCvs` and `TooManyTailoringRuns` do, and both are **soft**:
      two concurrent requests may overshoot by one, and that is accepted rather than locked.
    - The render timeouts, the output cap and the stylesheet. All four are adapter configuration,
      and the domain does not read settings (CLAUDE.md: `os.environ` is read in exactly one place).
    - Staleness relative to the **run**. `was_requested_for` is a comparison whose other side the
      caller supplies, because the aggregate cannot see the run — the same reason `TailoringRun`
      cannot raise `TailoringRunConcurrentlyModified` itself.
    """

    # Class-level annotations only (no assignment): the `__init__` below sets nothing, so this is
    # how `mypy --strict` learns the types of the fifteen attributes `request` and the three
    # transitions set directly on the instance and the properties below read back. SQLAlchemy's
    # imperative mapping targets these exact names, with `_recorded_events` deliberately not among
    # them (it is an in-memory outbox, not a persisted fact).
    _id: ExportJobId
    _guest_session_id: GuestSessionId
    _tailoring_run_id: TailoringRunId
    _document: TailoredDocumentKind
    _format: ExportFormat
    _run_version: int
    _status: ExportJobStatus
    _failure_reason: ExportFailureReason | None
    _file_key: FileRef | None
    _byte_size: int | None
    _render_duration_ms: int | None
    _requested_at: datetime
    _started_at: datetime | None
    _completed_at: datetime | None
    _version: int

    def __init__(self) -> None:
        """Takes nothing and does nothing. Build an `ExportJob` with `request`.

        **An empty constructor looks like something to delete, so here is why it must stay.** The
        obvious design defines no `__init__` at all and relies on `object.__init__` rejecting
        keyword arguments — that absence would be the entire mechanism behind "`request` is the only
        constructor", which XJ-1, XJ-2, XJ-3 and XJ-9 all rest on.

        The absence stops working the moment the class is mapped, which this one will be.
        `registry.map_imperatively` installs a default constructor on a mapped class *that does not
        define one*, and that constructor accepts the **mapped attribute names**. So with the
        mapping registered and no `__init__` here, this would be perfectly legal:

            ExportJob(_status=ExportJobStatus.READY)

        — a second way to build one, setting the status directly while holding no file, no size and
        no duration, which is precisely the state XJ-3 exists to make unrepresentable. It would also
        skip `request` entirely, leaving a job with no owner session, no run, and a format nothing
        checked against XJ-2. The identical hole was caught in `JobPosting` the hour its mapping was
        registered; `domain/posting/job_posting.py` carries the full account. AC-2 tests both halves
        of it here.

        Defining a **no-argument** `__init__` restores the guarantee exactly, and is the smallest
        thing that does. `map_imperatively` leaves a user-defined constructor alone, so the mapper's
        kwargs-accepting one is never installed, and any argument — public property name or private
        mapped name — is now a `TypeError` from Python's own signature check. The body stays empty
        because there is genuinely nothing to initialise: `request` assigns all fifteen attributes
        itself, and `RecordsEvents.record` creates its buffer lazily.

        Three things this deliberately does **not** do, each rejected for a reason:

        - It does not `raise`. A raising `__init__` would also break `request`, because a mapped
          class must be instantiated through `cls()` — SQLAlchemy's instrumentation wraps
          `__init__` and that wrapper is what attaches `_sa_instance_state`. Bypassing it with
          `cls.__new__(cls)` produces `AttributeError: 'NoneType' object has no attribute 'set'` on
          the first attribute assignment. Verified in 1.2, not guessed.
        - It does not take a private sentinel to distinguish "called by `request`" from "called by a
          stranger". That would work, and it would be persistence leaking into the domain: a
          parameter existing solely to defeat a constructor that an ORM installs.
        - It does not make this class the only line of defence. `export_job` carries seven CHECK
          constraints and a `UNIQUE`, so even a malformed in-memory instance cannot be stored
          (AC-4). Two independent mechanisms, which is what XJ-3 deserves.
        """

    # SQLAlchemy's imperative mapping does not need a constructor at all: it rehydrates a mapped
    # instance through `__new__`, instrumenting `__dict__` directly, and never calls `__init__` on
    # the load path (ADR-0007). The constructor above is for *application* code only.

    @classmethod
    def request(
        cls,
        *,
        id: ExportJobId,
        guest_session_id: GuestSessionId,
        tailoring_run_id: TailoringRunId,
        document: TailoredDocumentKind,
        format: ExportFormat,
        run_version: int,
        requested_at: datetime,
    ) -> ExportJob:
        """The only constructor. Sets `status = QUEUED`, every outcome attribute to `None`,
        `version = 1`, and records exactly one `ExportRequested`.

        Refuses, before anything exists:

        - an **inline** format (`md`, `txt`) → `ExportFormatNotQueued` (XJ-2, X-15). This is the one
          constructor in the codebase that rejects a member of its own enum, and it is why this
          aggregate shares no base class with the other three.
        - a `run_version` below 1 → `InvalidRunVersion` (XJ-8).

        Keyword-only, because three of its seven parameters are UUIDs of different things. The typed
        value objects already make a transposition a `mypy` error; keyword-only makes the call site
        say which is which to a human reading a diff, which is the other half of the same defence.

        Named `request` rather than `create` or `render`: the ubiquitous language is that a visitor
        *requested* an export. Nothing has rendered — a worker renders it, later, with
        `mark_started` — and calling this `render` would make `started_at` and `RENDERING` mean two
        different things in two places.

        **`run_version` is a parameter rather than something read from a run**, because the
        aggregate never sees the run (see "Deliberately not invariants"). The use case passes
        `run.version` at the moment of the click; from then on the job renders *that* version or
        fails `source_changed`.

        Both refusals happen **before `cls()`**, so a job that breaks XJ-2 or XJ-8 never exists even
        momentarily — there is no half-built instance for an `except` block somewhere to catch and
        keep.
        """
        if format.delivery is ExportDelivery.INLINE:
            raise ExportFormatNotQueued(format)
        if run_version < 1:
            raise InvalidRunVersion(f"run_version must be >= 1, got {run_version} (XJ-8)")

        job = cls()
        job._id = id
        job._guest_session_id = guest_session_id
        job._tailoring_run_id = tailoring_run_id
        job._document = document
        job._format = format
        job._run_version = run_version
        job._status = ExportJobStatus.QUEUED
        job._failure_reason = None
        job._file_key = None
        job._byte_size = None
        job._render_duration_ms = None
        job._requested_at = requested_at
        job._started_at = None
        job._completed_at = None
        # XJ-6: `version` is 1 at request; every transition after this bumps it by exactly one.
        job._version = 1

        job.record(
            ExportRequested(
                export_job_id=id,
                guest_session_id=guest_session_id,
                tailoring_run_id=tailoring_run_id,
                document=document,
                format=format,
                run_version=run_version,
                occurred_at=requested_at,
            )
        )
        return job

    def _guard_outcome_not_yet_decided(self) -> None:
        """The bottom two rows of the transition table, written once rather than three times: from
        `READY` or `FAILED`, all three transitions raise `ExportAlreadyDecided` (XJ-4). Two copies
        of an invariant is one copy that gets fixed and one that does not — the call
        `TailoringRun._guard_outcome_not_yet_decided` and `BaseCv` both make.

        Deliberately checked **before** each method's own status rule, so a redelivered task calling
        `mark_started` on a job that is already `ready` is told the outcome is decided rather than
        that the job is merely already started. The first is the fact the caller needs: one says
        "someone else finished this", the other says "someone else is still on it".
        """
        if self._status in (ExportJobStatus.READY, ExportJobStatus.FAILED):
            raise ExportAlreadyDecided(
                f"the outcome of {self._id!r} was already decided as {self._status!r}"
            )

    def _guard_completed_at(self, at: datetime) -> None:
        """XJ-5's second half, shared by `mark_ready` and `mark_failed`: an outcome may not be
        recorded as happening before the render started, or — when the job never started — before it
        was requested.

        The `None` branch is reachable only from `mark_failed`, because `mark_ready` is legal only
        from `RENDERING` and `RENDERING` always carries a `started_at`. It is written as a fallback
        rather than as an assertion precisely so that the one caller that *can* be in that state
        gets an honest floor instead of no check at all: a `not_queued` failure recorded before its
        own job was requested is the same negative duration XJ-5 exists to refuse.
        """
        floor = self._requested_at if self._started_at is None else self._started_at
        if at < floor:
            raise InvariantViolated(
                "completed_at must be >= started_at, or >= requested_at when the job never "
                "started (XJ-5)"
            )

    def mark_started(self, at: datetime) -> None:
        """A worker picked this job up and is about to render it: `queued → rendering`.

        Sets `started_at`, bumps `version` (XJ-6) and records `ExportStarted`. Raises
        `ExportAlreadyStarted` from `rendering` and `ExportAlreadyDecided` from either terminal
        state; `InvariantViolated` if `at` is before `requested_at` (XJ-5).

        The refusal from `rendering` is what makes a **sequentially** redelivered task idempotent
        (AC-18). Two deliveries genuinely in flight at once both read `queued` and both pass it;
        what stops the second is the `version` bump here plus `ExportJobConcurrentlyModified` on its
        `save` (AC-6, X-30) — which is why the bump is an invariant and not a detail.
        """
        self._guard_outcome_not_yet_decided()
        if self._status is ExportJobStatus.RENDERING:
            raise ExportAlreadyStarted(f"{self._id!r} is already rendering")
        if at < self._requested_at:
            raise InvariantViolated("started_at must be >= requested_at (XJ-5)")

        self._status = ExportJobStatus.RENDERING
        self._started_at = at
        # XJ-6: with the generator off, the row's version check only detects a race this bump moved
        # past — a transition that forgets this line has no concurrency protection (ADR-0015 §3).
        self._version += 1

        self.record(ExportStarted(export_job_id=self._id, occurred_at=at))

    def mark_ready(self, *, byte_size: int, render_duration_ms: int, at: datetime) -> None:
        """The render produced bytes and they are on the volume: `rendering → ready`.

        Sets `file_key`, `byte_size`, `render_duration_ms` and `completed_at` together (XJ-3), bumps
        `version`, and records `ExportReady`. Raises `ExportNotRendering` from `queued` and
        `ExportAlreadyDecided` from either terminal state; `InvariantViolated` if `at` is before
        `started_at` (XJ-5).

        **It takes no `FileRef`, and the absence is the invariant.** The aggregate writes
        `_file_key = self.storage_ref`, which is a pure function of its own id and its own format
        (XJ-7) — so the row and the file can never disagree, because **neither side chose the
        name**. The use case reads `job.storage_ref` *before* the render to know where to `put` the
        bytes, and writes nothing back; there is no parameter through which a caller could store one
        key and write another. That is ADR-0011 §1 turned from a convention into something
        unrepresentable, and it is what lets 1.6 find every file from a row — or reconstruct one
        from an id alone — without a lookup table (AC-5, AC-23, ADR-0016's amendment).

        `byte_size` and `render_duration_ms` are required rather than optional for XJ-3's sake and
        for PRD §8's: a `ready` job that cannot say how big its file is or how long it took is a
        success-rate and latency budget with no evidence behind it.
        """
        self._guard_outcome_not_yet_decided()
        if self._status is not ExportJobStatus.RENDERING:
            raise ExportNotRendering(f"{self._id!r} is {self._status!r}, not rendering")
        self._guard_completed_at(at)

        self._status = ExportJobStatus.READY
        # XJ-7: the key is computed, never passed. The row and the file cannot disagree, because
        # neither side chose the name.
        self._file_key = self.storage_ref
        self._byte_size = byte_size
        self._render_duration_ms = render_duration_ms
        self._completed_at = at
        # XJ-6, as in `mark_started` above.
        self._version += 1

        self.record(
            ExportReady(
                export_job_id=self._id,
                format=self._format,
                byte_size=byte_size,
                render_duration_ms=render_duration_ms,
                occurred_at=at,
            )
        )

    def mark_failed(self, reason: ExportFailureReason, at: datetime) -> None:
        """The job ended without a file: `queued → failed` or `rendering → failed`.

        Sets `failure_reason` and `completed_at`, bumps `version`, and records `ExportFailed`.
        Raises `ExportAlreadyDecided` from either terminal state; `InvariantViolated` if `at` is
        before `started_at` (XJ-5), or before `requested_at` when the job never started.

        Legal from `queued` for exactly one reason — a failed enqueue (`not_queued`, X-22) — which
        the class docstring's table spells out. It accepts all nine reasons, including the four
        nothing raises: the aggregate records the fact of a failure, not the fact of an exception.

        There is no status check beyond the decided guard, and the absence is the `queued` cell of
        the table rather than an omission: both non-terminal statuses may fail.
        """
        self._guard_outcome_not_yet_decided()
        self._guard_completed_at(at)

        self._status = ExportJobStatus.FAILED
        self._failure_reason = reason
        self._completed_at = at
        # XJ-6, as in the two transitions above. `started_at` is deliberately left untouched: on the
        # `queued → failed` path it stays `None`, because no worker ever began a render and a
        # timestamp invented to satisfy a state machine lies to every latency measurement built on
        # it.
        self._version += 1

        self.record(ExportFailed(export_job_id=self._id, reason=reason, occurred_at=at))

    @property
    def storage_ref(self) -> FileRef:
        """Where this job's file lives, or will live: `FileRef.for_export(id, format)`.

        A pure function of two immutable fields, **available from construction** — before the render
        and regardless of status — which is what lets the worker `put` the bytes under a key the
        aggregate has not yet committed to a row, and lets `mark_ready` then write that exact key
        with no argument to get wrong (XJ-7). Never `None`, because both inputs are required and
        XJ-2 guarantees the format is one the grammar admits.

        Distinct from `file_key` one property down, and the pair is deliberate: this is *the key
        this job's file would have*, and `file_key` is *the key the row records it does have*. The
        first is a computation, the second is a fact, and AC-5 asserts they are equal after
        `mark_ready` rather than assuming it.
        """
        return FileRef.for_export(self._id, self._format)

    def is_stale(self, now: datetime, stale_after: timedelta) -> bool:
        """Whether this job claims to be `rendering` but no worker can still be on it — a pure query
        that changes nothing and records no event.

        **This is the one home for the staleness rule**, asked by two callers who hold no copy of
        it: `RenderExportJob`, when a redelivered task finds its job already `rendering`, and the
        beat sweep that exists because redelivery alone does not bring a lost job back (X-29). They
        do different things with the answer — the worker returns `SKIPPED`, the sweep records
        `abandoned` — which is why this is a query and not an `abandon_if_stale` command: the part
        they share is the judgement, not what follows it.

        The rule, cell by cell: `QUEUED` → `False` (no worker to have lost); `READY` / `FAILED` →
        `False` (decided once; a decided job is not stale, it is over); `RENDERING` → `True` when
        `started_at is None`, or when `now - started_at > stale_after`.

        **Strict `>`.** A job exactly `stale_after` old is still fresh; it goes stale the second
        after. The clock is whole-second by contract (`domain/shared/clock.py`), so that edge is a
        single testable instant rather than a race.

        **The `None` fold lands on the stale side.** `started_at` cannot be `None` while the status
        is `RENDERING` — `mark_started` writes both in one breath — but the type cannot say so and a
        hand-written `UPDATE` can make it true. A job that claims to be rendering and cannot say
        since when is precisely "nobody is coming back for it"; folding it to fresh would leave it
        `rendering` for ever, which is the outcome this rule exists to prevent.

        `now` is an argument because the domain has no clock. `stale_after` is an argument rather
        than a constant because it is configuration (`export_stale_after_seconds`), and the domain
        does not read settings. Its one hard constraint lives outside this method, where it can be
        seen: the window must sit above the task's hard time limit, so a render still in flight can
        never be judged stale — which is what the second startup guard in `create_celery` refuses to
        boot without (D3). This method cannot know that limit and does not pretend to.
        """
        if self._status is not ExportJobStatus.RENDERING:
            return False
        # The fold, on the stale side for the reason in the docstring above.
        if self._started_at is None:
            return True
        return now - self._started_at > stale_after

    def was_requested_for(self, run_version: int) -> bool:
        """Whether this job was requested for the run version it is being compared against — i.e.
        `self.run_version == run_version`.

        A method rather than an inlined `==` at its three call sites (the idempotent lookup in
        `RequestExport`, the source check in `RenderExportJob`, and the `current` flag the API
        computes) because *current* and *stale* are the slice's ubiquitous language and a bare
        integer comparison in a router does not say either word. It takes the run's version as an
        argument because the aggregate cannot see the run; the caller holds both sides.

        **The comparison is deliberately an over-approximation.** A run has one version covering
        both of its documents, so an edit to the cover letter marks a CV export stale too (ADR-0016
        (b)). That is accepted: the cost is an occasional needless re-render, and the alternative —
        a per-document version — is a second counter that the optimistic-concurrency column would
        then have to agree with.
        """
        return self._run_version == run_version

    @property
    def id(self) -> ExportJobId:
        return self._id

    @property
    def guest_session_id(self) -> GuestSessionId:
        return self._guest_session_id

    @property
    def tailoring_run_id(self) -> TailoringRunId:
        return self._tailoring_run_id

    @property
    def document(self) -> TailoredDocumentKind:
        return self._document

    @property
    def format(self) -> ExportFormat:
        return self._format

    @property
    def run_version(self) -> int:
        """The run's `version` at the moment this export was requested (XJ-9, immutable).

        Not this job's own `version` one property down, and the two integers sitting on one
        aggregate is the single most confusable thing in this class: this one is a *reference to
        another aggregate's state*, read once and frozen, and the other is this job's own
        optimistic-concurrency counter. Neither is ever assigned from the other.
        """
        return self._run_version

    @property
    def status(self) -> ExportJobStatus:
        return self._status

    @property
    def failure_reason(self) -> ExportFailureReason | None:
        return self._failure_reason

    @property
    def file_key(self) -> FileRef | None:
        """The key of this job's file — `None` until `mark_ready` writes it from `storage_ref`.

        `None` is the honest answer before the bytes exist: a key on a `queued` job would name a
        file nothing has written, and 1.6's retention sweep would then be unlinking paths that were
        never created.
        """
        return self._file_key

    @property
    def byte_size(self) -> int | None:
        return self._byte_size

    @property
    def render_duration_ms(self) -> int | None:
        return self._render_duration_ms

    @property
    def requested_at(self) -> datetime:
        return self._requested_at

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    @property
    def completed_at(self) -> datetime | None:
        return self._completed_at

    @property
    def version(self) -> int:
        """This job's optimistic-concurrency counter: 1 at request, plus one per transition (XJ-6).

        Read by the mapping as `version_id_col` with `version_id_generator=False`, which means the
        database only *detects* a race that this number was bumped past. There is no setter; nothing
        outside the three transitions writes it. See `run_version` above for the other integer.
        """
        return self._version
