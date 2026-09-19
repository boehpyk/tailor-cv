"""Ports the `export` context needs from the outside world, in the domain's own language.

None of the three protocols below names a library, an HTTP detail, a broker, a task name, a
stylesheet, a page size, a timeout or a retry count — that is adapter business (ADR-0004, ADR-0017).
`ExportJobRepository` is implemented in `infrastructure/persistence/repositories/export/export_job.py`,
`DocumentRendererPort` in `infrastructure/export/`, and `ExportQueuePort` in `infrastructure/tasks/`.
None of those modules is imported here, and the dependency only ever points this way.

**These get no red-first cycle, and the reason is stated rather than left to look like an exemption**
(docs/sdlc.md §2, and the identical paragraph in `domain/tailoring/ports.py` and
`domain/posting/ports.py`). A `Protocol` has no behaviour: every method body is `...`, so there is
nothing that could fail an assertion, and a "test" of one would either assert that Python still has
ellipses or silently test whichever adapter it imported to stand in. What proves these are right is
that the adapters satisfy them — each carries an `if TYPE_CHECKING:` structural-conformance
assertion that makes `mypy --strict` do the checking — and that the use cases compile against them.
Verification here is by inspection, and for `DocumentRendererPort` the thing to inspect is written
out in its own docstring.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId


class ExportJobRepository(Protocol):
    """Persistence for the `ExportJob` aggregate.

    Mostly the same shape as `TailoringRunRepository` (`domain/tailoring/ports.py`), including the
    `get`-raises / `find`-returns-`None` asymmetry that every repository in this codebase documents.
    That asymmetry is deliberate and not an inconsistency to "fix": `get` is called where the caller
    already believes the row exists — the API's read path, holding an id out of a URL it is about to
    authorize — while `find` is the **worker's** lookup, where a job id that has stopped existing
    between the enqueue and the pickup is an ordinary branch rather than an error the retry machinery
    should take seriously (a guest session purged at the 24-hour mark cascades its jobs away while a
    message for one of them is still in Redis, ADR-0006).

    `get` does **not** check ownership. That is `ExportJobNotOwnedBySession`, a use-case decision: a
    repository that silently filtered by session would make the authorization rule invisible at the
    call site, and invisible rules are the ones a second entry point forgets — and this context has
    three entry points onto one job (poll, download, list).

    Two methods here have no counterpart in any earlier repository, and each exists for exactly one
    caller; see their own docstrings. `list_stale_rendering` is 1.3's `list_stale_running` renamed
    for the status it filters, and its contract is carried across in full rather than
    cross-referenced.
    """

    def next_identity(self) -> ExportJobId:
        """Mint an id for an `ExportJob` that does not exist yet.

        Synchronous, unlike everything else here: identity is application-assigned (UUIDv7,
        ADR-0007) and needs no I/O. That is what lets the aggregate be fully valid before it ever
        meets the database, which in turn is what makes the domain tests in `tests/unit/export/`
        possible without one.

        It does a second job in this context that it does nowhere else: the id is the *only* argument
        the queued task ever receives (ADR-0014 §5), **and** it is the sole input to the job's storage
        key (`FileRef.for_export`, XJ-7). Minting it here is therefore what makes the file's name
        known before the row is committed and before a single byte is rendered.
        """
        ...

    async def add(self, job: ExportJob) -> None: ...

    async def save(self, job: ExportJob) -> None:
        """Persist the current state of a job this repository already handed out.

        Says nothing about transactions. `save` means "make this the state you will hand back next
        time"; **when that becomes durable is the caller's boundary**, not this port's — which
        matters here for the same reason it did for a run: `RenderExportJob` needs two commits rather
        than one, because `rendering` has to be visible to a polling client *while* the render is
        still in flight.

        Raises `ExportJobConcurrentlyModified` when the row has changed since this aggregate was
        loaded (its `version` no longer matches — ADR-0015 §3, XJ-6). The caller decides what that
        means: the worker returns `SKIPPED` **before rendering**, the sweep counts it and continues,
        the API answers 409. That early return is what makes two simultaneous deliveries of one job
        render exactly once (AC-6): both read `queued`, both pass `ExportAlreadyStarted`, and the
        loser loses here rather than after spending the worker seconds.

        The error is domain-defined and adapter-raised, exactly like `ExportNotQueued` from the queue
        adapter below and `DocumentRenderFailed` from the renderer — the port names the failure in
        the domain's language, and the adapter translates whatever the vendor throws (here
        SQLAlchemy's `StaleDataError`, which nothing outside the repository ever sees). The aggregate
        cannot raise this itself: it can see only its own `version`, never the row's.
        """
        ...

    async def get(self, job_id: ExportJobId) -> ExportJob:
        """Raises `ExportJobNotFound` if no `ExportJob` with this id exists.

        The API's read path — the poll and the download both ask for a specific job, so "there is no
        such job" is an exceptional answer. Ownership is checked one layer up; see the class
        docstring.
        """
        ...

    async def find(self, job_id: ExportJobId) -> ExportJob | None:
        """Look up a job by id, returning `None` when there is none. **This is the worker's lookup.**

        The counterpart to `get` above, and the reason the asymmetry earns a second method rather
        than a second call site for the first one: the task is handed a job id that may legitimately
        have stopped existing between the enqueue and the pickup, and `task_acks_late=True` makes
        redelivery of an already-purged id real rather than theoretical.

        So absence is an **ordinary branch** here — the task returns `MISSING`, logs one line and
        stops — where it is an exception in the API's read path. Raising would turn a routine,
        expected outcome into an error the worker's retry machinery would take seriously, and a
        retried lookup of a row that is gone for ever is a loop with no exit.
        """
        ...

    async def list_for_run(self, run_id: TailoringRunId) -> Sequence[ExportJob]:
        """Every `ExportJob` requested for `run_id`, **newest first**.

        **One caller: the list endpoint** (`GET /api/tailoring-runs/{id}/exports`), which exists so a
        browser refresh can reattach to every in-flight export of a run in **one** request instead of
        four polls against ids it no longer holds. A run has two documents and two queued formats, so
        four is the realistic maximum in flight and the reason this is a list rather than a lookup.

        An empty sequence when the run has no exports — never an error. "Nothing has been exported
        yet" is the ordinary state of every run that has just finished, and it is what the workspace
        renders on first paint.

        Keyed on the **run**, not on the session, although every job also carries a session id. The
        endpoint is nested under the run and the caller has already authorized that run; filtering by
        session as well would make this read enforce an ownership rule the use case is responsible
        for, which is the same objection that keeps the check out of `get`.
        """
        ...

    async def find_latest_for_key(
        self, run_id: TailoringRunId, document: TailoredDocumentKind, format: ExportFormat
    ) -> ExportJob | None:
        """The most recently requested job for the (run, document, format) key, or `None`.

        **One caller: `RequestExport`'s idempotency check** (ADR-0016 (b)). A second click on
        "Download PDF" must not buy a second render, so the use case looks the key up, and — if the
        job it finds was requested for the run's current version (`was_requested_for`) and has not
        failed — returns that job with 200 instead of creating another with 202. A job requested for
        an older version is `source_changed`, and the answer is a **new** job at the current version:
        nothing is ever re-pointed (XJ-9).

        **"Latest" is `requested_at` descending, ties broken by id**, because the key is deliberately
        *not* unique — "Export again" after an edit is a second row for the same three fields, and
        keeping both is what makes the job table a history rather than a cache. Without a total order
        the idempotent lookup would be free to pick either of two jobs requested in the same whole
        second and answer differently on two identical requests.

        The **run version is not part of the key**, and that is the point: the use case needs to see
        the stale job in order to decide it is stale. A lookup that included the version would return
        `None` for exactly the case that needs an answer.
        """
        ...

    async def count_for_session(self, sid: GuestSessionId) -> int:
        """How many export jobs `sid` owns, for the `TooManyExportJobs` check.

        A separate method rather than `len(...)` over a list, so the SQL adapter can answer with
        `COUNT(*)`: the cap exists to bound a runaway loop, and materializing every row of a session
        that is *already* looping to produce one integer would make the defence cost more the worse
        the abuse gets.

        The cap is **soft** by decision, exactly as `TooManyBaseCvs` and `TooManyTailoringRuns` are:
        two concurrent requests can both pass the count and overshoot by one, and that is accepted
        rather than locked. What it bounds is a runaway loop and the volume it would fill, not an
        exact number of rows.
        """
        ...

    async def list_stale_rendering(
        self, started_before: datetime, limit: int
    ) -> Sequence[ExportJob]:
        """`RENDERING` jobs that no worker can still be on, for `AbandonStaleExportJobs` — the beat
        sweep that records a job whose worker was lost (X-29).

        **Which jobs.** Status `RENDERING`, and either `started_at < started_before` **or no
        `started_at` at all**. That is `ExportJob.is_stale` expressed as a filter, fold included: the
        caller passes `now - stale_after`, and `started_at < now - stale_after` is the same claim as
        `now - started_at > stale_after`. The `None` half is not a state this codebase writes —
        `mark_started` sets both fields together — but a filter that left out a row the rule calls
        stale would hide it from the caller's re-check, and that row would stay `RENDERING` for ever.

        **Bounded, and in a total order.** At most `limit` jobs, oldest `started_at` first, a job
        with no `started_at` counting as the oldest of all (`NULLS FIRST` in SQL terms, where an
        ascending sort would otherwise put it last), ties broken by id. The bound is the caller's
        decision: a backlog after an outage must not load every row in one tick. The total order is
        what makes the bound cut in the same place every time, since whole-second timestamps make
        ties ordinary.

        **A listed job may already be decided by the time the caller reaches it.** No lock is taken:
        a redelivered `RenderExportJob` can record the same job between this read and the caller's
        write. The caller re-checks `is_stale` on each job and owns that race; its docstring records
        why the outcome is benign.

        Says nothing about transactions, exactly as `save` does not.
        """
        ...


class DocumentRendererPort(Protocol):
    """Turn one document's Markdown into the bytes of one format.

    **This port is the point of this slice, so read the signature for what is missing — that list is
    the specification** (ADR-0004, ADR-0017). No HTML. No CSS, no stylesheet, no template, no font,
    no page size, no margin. No WeasyPrint, no `python-docx`, no `markdown-it`, no `nh3`. No token,
    no token stream, no walker, no ProseMirror node. No timeout, no retry count, no output cap. No
    file, no path, no storage key, no filename.

    Every one of those is real and load-bearing, and every one of them lives in
    `infrastructure/export/`. If any appeared here it would be a rename rather than a port: the
    domain would hold opinions about one renderer's pipeline, and swapping WeasyPrint for a print
    service would become a change to the business model. (Naming them in this paragraph is the point
    of the paragraph; naming one in a parameter or a return type is the violation.)

    **`markdown: str` is not a vendor leaking in, and the distinction is worth being precise about,
    because HTML in this signature would be.** Markdown is the domain's *own stored format* — ADR-0015
    §2 made it the ubiquitous language of a tailored document, it is what the model returns, what the
    row holds and what a revision writes back. HTML is one adapter's *intermediate*, produced for one
    of the four formats and discarded in the same function; DOCX walks tokens and never sees it, and
    TXT and Markdown never build it at all. A port speaks the format the business stores, not the
    format one implementation passes through.

    What the domain cares about is exactly this: *given a document's Markdown, which document it is
    and which format is wanted, give me bytes or a failure I have a name for.*
    """

    async def render(
        self, markdown: str, *, document: TailoredDocumentKind, format: ExportFormat
    ) -> bytes:
        """Render one document into one format's bytes.

        **`markdown: str` rather than `TailoredCv | CoverLetter`.** Both value objects already hold
        Markdown validated to the same bounds, so a union would buy no extra guarantee — and it would
        have to grow a member every time a document kind appears, which is a port signature churning
        for a reason that has nothing to do with rendering. The caller passes `document.value`;
        `document=` carries the kind, which the walkers need because a CV and a letter are titled
        differently.

        `document` and `format` are **keyword-only** because all three parameters would otherwise be
        positional strings — `markdown`, a `StrEnum` and a `StrEnum` — and a transposition of the last
        two is a `mypy` error only by luck of their types. Keyword-only makes the call site say which
        is which to a human reading a diff.

        **`async`, and the adapter runs the synchronous libraries in `asyncio.to_thread` under
        `asyncio.wait_for`.** WeasyPrint, `python-docx` and `markdown-it-py` are all synchronous, and
        the two reasons this must not be a plain `def` are different in the two places it is called:

        - In the **API** (the inline path) it is what keeps even a cheap parse off the event loop. A
          CPU-bound call in an async route stalls *every* concurrent user, including the ones that
          "aren't real work" — 1.1's DOCX sniff was a zip directory read and it stalled the loop
          374 ms (CLAUDE.md). It looks fine with one user and collapses at five, with no error and
          nothing logged.
        - In the **worker** (the queued path) it is what makes a timeout enforceable at all.
          `wait_for` can only cancel an await; a synchronous call would own the thread until it
          returned, and `render_timed_out` would be a failure reason nothing could ever record. The
          thread left behind after a cancel is the hard time limit's problem, which is why the stale
          window must sit above that limit (the second startup guard in `create_celery`).

        Raises a `DocumentRenderFailed` subclass (`domain/export/errors.py`) on **every** failure —
        never a bare exception from whatever library, parser or layout engine the adapter happens to
        use underneath. The adapter guarantees that **structurally**, with an `except Exception`
        floor beneath its specific translations, rather than with an allow-list of the vendor errors
        it happened to think of: an allow-list is a bet that you enumerated every way a library can
        fail on input a stranger chose, and that bet loses. The CV-extractor sweep recorded in
        CLAUDE.md found `KeyError`, `AttributeError`, `ValueError` and `LimitReachedError` escaping
        through exactly such a list, and there are **four** libraries behind this one port.
        `Exception`, never `BaseException` — `asyncio.CancelledError` must still cancel (X-36).

        The two fates of that exception family are decided by the caller, not here: `RenderExportJob`
        catches it and records `mark_failed(exc.reason)` on a committed row, because a worker second
        was spent and there is an artifact to own (ADR-0014 §2); `RenderDocumentInline` lets it
        propagate to a 500, because on that path there is no row and nothing to record it on (X-5).
        """
        ...


class ExportQueuePort(Protocol):
    """Hand a requested export to whatever will actually render it.

    **This is the Constitution §4.2 `TaskQueuePort` role, made context-specific for the second
    time** — the same deviation from that section's naming that `TailoringQueuePort` made, decided in
    ADR-0014 §8 and recorded in ADR-0016 (d).

    A single generic `TaskQueuePort.enqueue(name: str, **kwargs)` would put the *broker's* vocabulary
    — task names and an untyped kwargs bag — into a file under `domain/`. That is precisely the "port
    that is a rename" failure: a Protocol that adds a layer of indirection while faithfully
    reproducing the vendor's concepts, and buying a type signature that says nothing.
    `enqueue(job_id)` says what the domain wants: *make this render happen, not necessarily now.*

    **This is ADR-0014 §8's second data point, not its promotion.** §8 predicted this port and
    predicted the temptation that arrives with it — two one-method Protocols with structurally
    identical signatures, and an obvious generic supertype sitting between them. The decision stands
    as written: *if a third one appears with an identical signature that is the moment to reconsider
    — not now, on the strength of two.* Two is a coincidence a shared base class would freeze into a
    rule, and the rule it would freeze guesses at what the third queue wants; the queues also differ
    in what a refusal costs (a run that never calls the model versus a render that never produces a
    file), which is the kind of difference a supertype cannot hold.
    """

    async def enqueue(self, job_id: ExportJobId) -> None:
        """Publish the job for rendering. The id is the only argument, which is what makes the task
        idempotent by construction: everything else it needs it reads back from the row, and the row
        was committed before this was called (commit-then-enqueue, ADR-0014 §5).

        Raises `ExportNotQueued` when the broker refuses or is unreachable (X-22). That is a plain
        `DomainError` and deliberately **not** a `DocumentRenderFailed`, for 1.3's reason with the
        money removed: **nothing was rendered**. The renderer was never reached, no worker second was
        spent and no bytes exist, so this is not an outcome of a render and does not belong in the
        taxonomy of ways a render can fail.

        The row is a separate question from the exception — the job is already committed as `queued`,
        so the router records it `failed` / `not_queued` in a second transaction and answers 503
        rather than leaving a job the client polls until it gives up. That is the one reason
        `mark_failed` is legal from `queued` at all (the class docstring's table).
        """
        ...
