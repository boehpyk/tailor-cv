"""The `TailoringRun` aggregate: one attempt to turn a base CV and a job posting into two documents.

Composes `RecordsEvents` (`domain/shared/events.py`) rather than inheriting a shared aggregate base
class, and shares **no** base class with `BaseCv` or `JobPosting` — CLAUDE.md is explicit that two
aggregates with the same shape do not get one. The shape is again similar (an id, an owner session, a
created-at, a status, a failure reason) and the similarity is again the trap: `BaseCv` has one
decision point and a two-way outcome; `JobPosting` cannot exist unless it succeeded; this one is a
four-state machine whose whole behaviour is the table below. No supertype could hold all three, and a
base class would have to guess which set of rules it enforces.

**This is the aggregate the slice exists to demonstrate.** The lesson is not that a run has a status
column — it is that the status is written **only** by three methods that say what happened
(`mark_started`, `mark_succeeded`, `mark_failed`) rather than by a settable field. A settable
`status` makes every caller responsible for the transition table; named transitions make the
aggregate responsible for it, once, where it can be tested exhaustively.

This module was written in two steps (docs/sdlc.md §2): a T4 skeleton of real signatures with
`NotImplementedError` bodies, so that `qa`'s T5 tests failed on their *assertions* rather than on an
`ImportError`, and then this — the T6 GREEN filling in `request` and the three transitions against
those recorded reds. Nothing else moved between the two steps, and nothing in the tests was touched
to get here.
"""

from __future__ import annotations

from datetime import datetime

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.domain.shared.events import RecordsEvents
from tailorcraft.domain.tailoring.errors import (
    TailoringAlreadyDecided,
    TailoringAlreadyStarted,
    TailoringNotRunning,
)
from tailorcraft.domain.tailoring.events import (
    TailoringRunFailed,
    TailoringRunRequested,
    TailoringRunStarted,
    TailoringRunSucceeded,
)
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocuments,
    TailoringFailureReason,
    TailoringRunId,
    TailoringRunStatus,
)


# NOT `slots=True`: this aggregate is later mapped by SQLAlchemy's *imperative* mapping
# (`registry.map_imperatively`), which instruments attributes on the instance `__dict__` — a
# `__slots__` class has none, so mapping would fail at wiring time rather than at import time, which
# is a much worse place to discover it. This is a deliberate departure from the value objects one
# module over, which are all `slots=True` because nothing ever maps them directly: a reader who has
# just written `slots=True` on ten value objects in `value_objects.py` and arrives here should find
# the reason rather than "fix" the inconsistency. `BaseCv` and `JobPosting` carry the same comment
# for the same reason, and the duplication is on purpose — a reader lands on one class, not on all
# three. The private-attribute names below are exactly what
# `infrastructure/persistence/mapping/tailoring/tailoring_run.py` will target, so renaming one here
# is a breaking change to that module too (ADR-0007).
class TailoringRun(RecordsEvents):
    """One attempt to tailor a base CV to a job posting: who asked, from which two inputs, where it
    stands, and — once decided — the two documents and what the call cost, or the reason it failed.

    **The legal-transition table.** This is the aggregate's whole behaviour and AC-3 tests all
    sixteen cells:

    | From \\ call | `mark_started` | `mark_succeeded` | `mark_failed` |
    |---|---|---|---|
    | `queued` | → `running` | `TailoringNotRunning` | → `failed` |
    | `running` | `TailoringAlreadyStarted` | → `succeeded` | → `failed` |
    | `succeeded` | `TailoringAlreadyDecided` | `TailoringAlreadyDecided` | `TailoringAlreadyDecided` |
    | `failed` | `TailoringAlreadyDecided` | `TailoringAlreadyDecided` | `TailoringAlreadyDecided` |

    **The one cell a reader will question is `mark_failed` from `queued`, and it is legal on
    purpose.** A run can fail before it ever starts, twice over (ADR-0014 §5). First: the row is
    committed and *then* the task is published, so a broker that refuses the publish leaves a
    committed run that can never run — the router records it `failed`/`not_queued` and answers 503
    (G-14), because leaving it `queued` forever is a run the client polls until it gives up. Second:
    a redelivered task can find a run that has been `running` past the stale window and that nobody
    is coming back for — the worker records it `failed`/`abandoned` (G-25). Neither may be recorded
    by first pretending the run started: `started_at` means *a worker began a call*, and a
    `started_at` invented to satisfy a state machine is a timestamp that lies to every latency
    measurement built on it.

    Invariants (technical-plan.md):

    - **TR-1** — A run always has exactly one owner session, one base CV id and one job posting id.
      `request` requires all three; there is no other constructor and no setter. The three ids are
      typed rather than bare `UUID`s precisely because this aggregate holds three of them and
      transposing two would produce a run tailoring the wrong person's CV with no error anywhere.
    - **TR-2** — `status == SUCCEEDED` **iff** `documents is not None`; `status == FAILED` **iff**
      `failure_reason is not None`. Never both, never neither. `mark_succeeded` and `mark_failed` are
      the only writers of either field, and the table above forbids a second decision. Mirrored as
      **two database `CHECK` constraints** (AC-4), because two methods do not bind a hand-written
      `UPDATE` or a `psql` session at 2 a.m.
    - **TR-3** — The outcome is decided **once**. Enforced by the bottom two rows of the table and
      `TailoringAlreadyDecided`. **This is what makes the Celery task idempotent under
      `task_acks_late`** (AC-10, ADR-0014 §6): a redelivered task calls `mark_started` on a run that
      is no longer `queued` and is refused, so no second call to Gemini is ever made, and a
      redelivery that arrives after an outcome cannot overwrite it. Idempotency here is a **domain**
      property, not a flag in a task — which is the reason the task declares no retry and needs no
      "already processed" bookkeeping. Anyone reaching for a retry flag on the task should read this
      row first: a second mechanism would not add safety, it would multiply attempts.
    - **TR-4** — `started_at >= requested_at`, and `completed_at >= started_at` when both are set.
      Guarded in each transition; raises `InvariantViolated` (`domain/shared/errors.py`), the same
      error `BaseCv`'s I-4 raises for the same class of mistake. Time that runs backwards inside one
      row is not a rounding problem; it is a duration that comes out negative in a percentile three
      weeks later, and the aggregate is the cheapest place to refuse it.
    - **TR-5** — A succeeded run has **both** documents. Half a result is a failure. Enforced by
      `TailoredDocuments` requiring both fields, so "succeeded with no cover letter" is
      **unconstructable rather than checked** — `mark_succeeded` takes the pair, never two optionals,
      so there is no branch anywhere that could admit one of them.
    - **TR-6** — The three references and `requested_at` are **immutable after creation**. There is
      no setter, and — the part worth recording — **there is no `retry()` and no `rerun()`**. Their
      absence is a decision, not an omission: "Try again" creates a *new* run, so that what a run
      cost, when it ran, which model wrote it and what it produced stay one immutable fact. A
      `rerun()` would silently overwrite a document the user may already have edited and would make
      the token spend of a run unanswerable. This row exists so that whoever is tempted to add one in
      slice 1.4 finds the reason here instead of an empty space that looks like an oversight.
    - **TR-7** — A succeeded run always carries `LlmCallMetrics`. `mark_succeeded` requires it, as a
      parameter rather than as a check. Without it, ADR-0004's "record duration and token counts on
      every run" is a promise the schema does not keep and Constitution §7's 15-second budget has no
      evidence behind it — a budget you cannot measure is a budget you no longer have.

    **Deliberately not invariants of `TailoringRun`:**

    - The per-session run cap and the "at most one active run per session" rule (G-9, G-10). Both
      span every run a session owns, which is a fact no single instance has access to — reaching for
      either from inside a constructor would mean a repository call in a constructor. They live in
      `RequestTailoringRun` with a comment there saying why, exactly as `TooManyBaseCvs` and
      `TooManyJobPostings` do, and both are **soft**: two concurrent requests may overshoot by one,
      and that is accepted rather than locked.
    - The per-attempt timeout, the retry count, the token budget and the model name. All four are
      configuration read by an adapter, and the domain must not read settings (CLAUDE.md: `os.environ`
      is read in exactly one place). This aggregate has no opinion about how its documents were
      obtained beyond the `ModelName` and `PromptVersion` recorded as *provenance* after the fact.
    - The 15-second budget itself. It is a target measured at the boundary — `make eval`'s p95 of
      `llm_duration_ms` plus the plumbing — not a rule an aggregate could enforce. An aggregate that
      refused a slow-but-successful run would throw away two documents the user already paid for.
    """

    # Class-level annotations only (no assignment): the `__init__` below sets nothing, so this is how
    # `mypy --strict` learns the types of the attributes `request` and the three transitions set
    # directly on the instance and the properties below read back. SQLAlchemy's imperative mapping
    # targets these exact names — sixteen mapped attributes, and `_recorded_events` deliberately not
    # among them (it is an in-memory outbox, not a persisted fact).
    _id: TailoringRunId
    _guest_session_id: GuestSessionId
    _base_cv_id: BaseCvId
    _job_posting_id: JobPostingId
    _status: TailoringRunStatus
    _failure_reason: TailoringFailureReason | None
    _tailored_cv: TailoredCv | None
    _cover_letter: CoverLetter | None
    _model_name: ModelName | None
    _prompt_version: PromptVersion | None
    _prompt_tokens: int | None
    _completion_tokens: int | None
    _llm_duration_ms: int | None
    _requested_at: datetime
    _started_at: datetime | None
    _completed_at: datetime | None

    def __init__(self) -> None:
        """Takes nothing and does nothing. Build a `TailoringRun` with `request`.

        **An empty constructor looks like something to delete, so here is why it must stay.** The
        obvious design defines no `__init__` at all and relies on `object.__init__` rejecting keyword
        arguments — that absence would be the entire mechanism behind "`request` is the only
        constructor", which TR-1, TR-2 and TR-6 all rest on.

        The absence stops working the moment the class is mapped, which this one will be at T19.
        `registry.map_imperatively` installs a default constructor on a mapped class *that does not
        define one*, and that constructor accepts the **mapped attribute names**. So with the mapping
        registered and no `__init__` here, this would be perfectly legal:

            TailoringRun(_status=TailoringRunStatus.SUCCEEDED)

        — a second way to build one, setting the status directly and holding no documents, which is
        precisely the state TR-2 exists to make unrepresentable. It would also skip `request`
        entirely, leaving a run with no owner session and no inputs. The identical hole was caught in
        `JobPosting` the hour its mapping was registered, by a test whose docstring had already named
        SQLAlchemy as a way someone might dismantle the rule quietly; `domain/posting/job_posting.py`
        carries the full account. AC-5 tests both halves of it here.

        Defining a **no-argument** `__init__` restores the guarantee exactly, and is the smallest
        thing that does. `map_imperatively` leaves a user-defined constructor alone, so the mapper's
        kwargs-accepting one is never installed, and any argument — public property name or private
        mapped name — is now a `TypeError` from Python's own signature check. The body stays empty
        because there is genuinely nothing to initialise: `request` assigns all sixteen attributes
        itself, and `RecordsEvents.record` creates its buffer lazily.

        Three things this deliberately does **not** do, each rejected for a reason:

        - It does not `raise`. A raising `__init__` would also break `request`, because a mapped
          class must be instantiated through `cls()` — SQLAlchemy's instrumentation wraps `__init__`
          and that wrapper is what attaches `_sa_instance_state`. Bypassing it with
          `cls.__new__(cls)` produces `AttributeError: 'NoneType' object has no attribute 'set'` on
          the first attribute assignment. Verified in 1.2, not guessed.
        - It does not take a private sentinel to distinguish "called by `request`" from "called by a
          stranger". That would work, and it would be persistence leaking into the domain: a
          parameter existing solely to defeat a constructor that an ORM installs.
        - It does not make this class the only line of defence. `tailoring_run` carries three CHECK
          constraints enforcing TR-2 and the completed-at rule, so even a malformed in-memory
          instance cannot be stored (AC-4). Two independent mechanisms, which is what TR-2 deserves.
        """

    # SQLAlchemy's imperative mapping does not need a constructor at all: it rehydrates a mapped
    # instance through `__new__`, instrumenting `__dict__` directly, and never calls `__init__` on
    # the load path (ADR-0007). The constructor above is for *application* code only.

    @classmethod
    def request(
        cls,
        *,
        id: TailoringRunId,
        guest_session_id: GuestSessionId,
        base_cv_id: BaseCvId,
        job_posting_id: JobPostingId,
        requested_at: datetime,
    ) -> TailoringRun:
        """The only constructor. Sets `status = QUEUED`, every optional attribute to `None`, and
        records exactly one `TailoringRunRequested`.

        Keyword-only, because four of its five parameters are ids and three of those four are UUIDs
        of different things. The typed value objects already make a transposition a `mypy` error;
        keyword-only makes the call site say which is which to a human reading a diff, which is the
        other half of the same defence.

        Named `request` rather than `create` or `start`: the ubiquitous language for what happens at
        this moment is that a visitor *requested* a run. Nothing has started — a worker starts it,
        later, with `mark_started` — and calling this `start` would make `started_at` and the
        `RUNNING` status mean two different things in two places.

        There is no validation here and the absence is deliberate rather than an omission: every
        invariant available at this moment is structural. TR-1 is the signature (all three references
        required, no defaults); TR-6 is the absence of a setter; TR-2 and TR-5 have nothing to say
        about a run with no outcome; TR-4's comparisons need a second timestamp that does not exist
        yet. `requested_at` is not compared against anything because there is nothing to compare it
        with — a run's own request time is the origin of its timeline. This constructor takes no bare
        primitive, so there is no `size_bytes > 0`-shaped check for it to grow (`BaseCv.upload` needs
        one because `int` has no rules of its own).
        """
        run = cls()
        run._id = id
        run._guest_session_id = guest_session_id
        run._base_cv_id = base_cv_id
        run._job_posting_id = job_posting_id
        run._status = TailoringRunStatus.QUEUED
        run._failure_reason = None
        run._tailored_cv = None
        run._cover_letter = None
        run._model_name = None
        run._prompt_version = None
        run._prompt_tokens = None
        run._completion_tokens = None
        run._llm_duration_ms = None
        run._requested_at = requested_at
        run._started_at = None
        run._completed_at = None

        run.record(
            TailoringRunRequested(
                tailoring_run_id=id,
                guest_session_id=guest_session_id,
                base_cv_id=base_cv_id,
                job_posting_id=job_posting_id,
                occurred_at=requested_at,
            )
        )
        return run

    def _guard_outcome_not_yet_decided(self) -> None:
        """The bottom two rows of the transition table, written once rather than three times: from
        `SUCCEEDED` or `FAILED`, every one of the three transitions raises `TailoringAlreadyDecided`
        (TR-3). Two copies of an invariant is one copy that gets fixed and one that does not, which
        is the same call `BaseCv._guard_extraction_not_yet_decided` makes.

        Deliberately checked *before* each method's own status rule, so that a redelivered task
        calling `mark_started` on a run that already succeeded is told the outcome is decided rather
        than that the run is merely already started — the first is the fact the caller needs.
        """
        if self._status in (TailoringRunStatus.SUCCEEDED, TailoringRunStatus.FAILED):
            raise TailoringAlreadyDecided(
                f"the outcome of {self._id!r} was already decided as {self._status!r}"
            )

    def _guard_completed_at(self, at: datetime) -> None:
        """TR-4's second half, shared by `mark_succeeded` and `mark_failed`: an outcome may not be
        recorded as happening before the run started, or — when the run never started — before it
        was requested.

        The `None` branch is reachable only from `mark_failed`, because `mark_succeeded` is legal
        only from `RUNNING` and `RUNNING` always carries a `started_at`. It is written as a fallback
        rather than as an assertion precisely so that the one caller that *can* be in that state gets
        the honest floor instead of no check at all: a `not_queued` or `abandoned` failure recorded
        before its own run was requested is the same negative duration TR-4 exists to refuse.
        """
        floor = self._requested_at if self._started_at is None else self._started_at
        if at < floor:
            raise InvariantViolated(
                "completed_at must be >= started_at, or >= requested_at when the run never "
                "started (TR-4)"
            )

    def mark_started(self, at: datetime) -> None:
        """Record that a worker picked the run up and is about to call the model: sets
        `status = RUNNING`, `started_at = at`, and records `TailoringRunStarted`.

        Legal **only** from `QUEUED`. Raises `TailoringAlreadyStarted` from `RUNNING` and
        `TailoringAlreadyDecided` from either terminal status, and `InvariantViolated` if
        `at < requested_at` (TR-4).

        The refusal from `RUNNING` is not a defence against a caller bug — it is the redelivery case
        (TR-3): `task_acks_late` means a task whose worker died mid-call comes back, and the second
        attempt finds its own run already started. Refusing here is exactly what stops a second paid
        call to Gemini for one button press (AC-10).
        """
        self._guard_outcome_not_yet_decided()
        if self._status is TailoringRunStatus.RUNNING:
            raise TailoringAlreadyStarted(f"{self._id!r} is already running")
        if at < self._requested_at:
            raise InvariantViolated("started_at must be >= requested_at (TR-4)")

        self._status = TailoringRunStatus.RUNNING
        self._started_at = at

        self.record(TailoringRunStarted(tailoring_run_id=self._id, occurred_at=at))

    def mark_succeeded(
        self, documents: TailoredDocuments, metrics: LlmCallMetrics, at: datetime
    ) -> None:
        """Record that the call returned two usable documents: sets `status = SUCCEEDED`, writes the
        two documents and the five metric values through to their private scalars, sets
        `completed_at = at`, and records `TailoringRunSucceeded`.

        Legal **only** from `RUNNING`. Raises `TailoringNotRunning` from `QUEUED` — there is no path
        from "queued" straight to two documents, because documents come from a call and a call is
        what `RUNNING` records — `TailoringAlreadyDecided` from either terminal status, and
        `InvariantViolated` if `at < started_at` (TR-4).

        Both parameters are required and neither is optional, which is how TR-5 and TR-7 are enforced
        as *signatures* rather than as checks: a caller with only a CV, or with two documents and no
        metrics, has nothing to pass.

        The event it records carries the metrics and the two character counts and **not** the
        documents (AC-22) — see `domain/tailoring/events.py` for why an event's field set is a log
        field set.
        """
        self._guard_outcome_not_yet_decided()
        if self._status is not TailoringRunStatus.RUNNING:
            raise TailoringNotRunning(f"{self._id!r} is {self._status!r}, not running")
        self._guard_completed_at(at)

        # Written through to the seven scalars rather than stored as the two composites: ADR-0007
        # maps value objects one per column, so `documents` and `metrics` are assembled on read (see
        # those two properties for the full reasoning behind the asymmetry, OQ-5).
        self._tailored_cv = documents.cv
        self._cover_letter = documents.cover_letter
        self._model_name = metrics.model
        self._prompt_version = metrics.prompt_version
        self._prompt_tokens = metrics.prompt_tokens
        self._completion_tokens = metrics.completion_tokens
        self._llm_duration_ms = metrics.duration_ms
        self._status = TailoringRunStatus.SUCCEEDED
        self._completed_at = at

        self.record(
            TailoringRunSucceeded(
                tailoring_run_id=self._id,
                model=metrics.model,
                prompt_version=metrics.prompt_version,
                prompt_tokens=metrics.prompt_tokens,
                completion_tokens=metrics.completion_tokens,
                duration_ms=metrics.duration_ms,
                cv_character_count=documents.cv.character_count,
                cover_letter_character_count=documents.cover_letter.character_count,
                occurred_at=at,
            )
        )

    def mark_failed(self, reason: TailoringFailureReason, at: datetime) -> None:
        """Record that the run ended without documents: sets `status = FAILED`,
        `failure_reason = reason`, `completed_at = at`, and records `TailoringRunFailed`.

        Legal from **`QUEUED` as well as `RUNNING`** — see the class docstring for the two ways a run
        fails before it starts (a refused enqueue, G-14; a stale redelivery, G-25) and why neither
        may be recorded by pretending it started. Raises `TailoringAlreadyDecided` from either
        terminal status, and `InvariantViolated` if `at < started_at` when `started_at` is set, or
        `at < requested_at` when it is not (TR-4).

        The five metric attributes stay `None` here, on every path. On most failures there is no
        successful call to describe, and a partially-filled metrics row would be worse than an absent
        one: it would show up in a token-spend total as a real number that nothing was paid for.

        This is the ADR-0004 shape and the deliberate contrast with `CaptureJobPosting`: a failed run
        is a **recorded state of the aggregate**, never an exception that escapes to a 500 with
        nothing on disk. `ExecuteTailoringRun` catches `TailoringFailed` and calls this; an
        application test asserts the recording rather than the propagation, so "fixing" it into a
        propagating error turns a test red.
        """
        self._guard_outcome_not_yet_decided()
        self._guard_completed_at(at)

        # No status check beyond the terminal guard above, and the absence is the `queued` cell of
        # the table: both non-terminal statuses may fail. `_started_at` is left exactly as it is —
        # `None` on the `queued` path — because a `started_at` invented to satisfy a state machine is
        # a timestamp that lies to every latency measurement built on it. The five metric scalars
        # stay `None` on every path for the reason in the docstring above.
        self._status = TailoringRunStatus.FAILED
        self._failure_reason = reason
        self._completed_at = at

        self.record(TailoringRunFailed(tailoring_run_id=self._id, reason=reason, occurred_at=at))

    @property
    def id(self) -> TailoringRunId:
        return self._id

    @property
    def guest_session_id(self) -> GuestSessionId:
        return self._guest_session_id

    @property
    def base_cv_id(self) -> BaseCvId:
        return self._base_cv_id

    @property
    def job_posting_id(self) -> JobPostingId:
        return self._job_posting_id

    @property
    def status(self) -> TailoringRunStatus:
        return self._status

    @property
    def failure_reason(self) -> TailoringFailureReason | None:
        return self._failure_reason

    @property
    def documents(self) -> TailoredDocuments | None:
        """The pair the model produced, assembled from the two private scalars — `None` until the run
        succeeds.

        **This property assembles rather than returns, and so does `metrics`. That is the one
        asymmetry in this aggregate and it is deliberate (OQ-5).** SQLAlchemy's obvious answer for a
        multi-column value object is `composite()`, and ADR-0007 forbids it: value objects map
        through `TypeDecorator`s, one per column. So `TailoredDocuments` is **not a mapped attribute
        at all**. The storage is seven scalars — two documents here, five metrics next door — the
        mapping (T19) targets those seven private names, and the domain still deals in two composite
        value objects because these two properties put them back together on read and
        `mark_succeeded` writes through to the scalars.

        The rejected alternative was to expose the seven optional value objects on the aggregate
        directly and drop the composites. It costs more than it saves: TR-5 becomes a two-part
        invariant and TR-7 a five-part one — each of them a rule that has to be re-checked rather
        than made unconstructable — and **every read site becomes a multi-part narrowing**
        (`if run.tailored_cv is not None and run.cover_letter is not None`) where today it is one
        `if run.documents is not None`. Seven optionals is also seven chances to set six of them.

        A reader arriving from the mapping module will see seven columns and one property and may
        want to tidy it into a composite; the seam is documented in both places for that reason.
        """
        if self._tailored_cv is None or self._cover_letter is None:
            return None
        return TailoredDocuments(cv=self._tailored_cv, cover_letter=self._cover_letter)

    @property
    def metrics(self) -> LlmCallMetrics | None:
        """What the call cost, assembled from the five private scalars — `None` until the run
        succeeds. See `documents` for why this is assembled rather than mapped (OQ-5, ADR-0007).

        All five are written together by `mark_succeeded` and are `NULL` together on every other
        path, so the `None` check below is a narrowing for `mypy` rather than a real branch: TR-7
        guarantees that a succeeded run has all five. It is written as an all-or-nothing test anyway,
        because the alternative — asserting the invariant with five `assert` statements — would trade
        a `None` return for an `AssertionError` in a read path, and a read path is the worst place to
        discover that a hand-written `UPDATE` broke a rule.
        """
        if (
            self._model_name is None
            or self._prompt_version is None
            or self._prompt_tokens is None
            or self._completion_tokens is None
            or self._llm_duration_ms is None
        ):
            return None
        return LlmCallMetrics(
            model=self._model_name,
            prompt_version=self._prompt_version,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            duration_ms=self._llm_duration_ms,
        )

    @property
    def requested_at(self) -> datetime:
        return self._requested_at

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    @property
    def completed_at(self) -> datetime | None:
        return self._completed_at
