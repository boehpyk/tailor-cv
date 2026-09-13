"""Ports the `tailoring` context needs from the outside world, in the domain's own language.

None of the three protocols below names a library, an HTTP detail, a broker, a task name, a model, a
timeout or a retry count — that is adapter business (ADR-0004). `TailoringRunRepository` is
implemented in `infrastructure/persistence/repositories/tailoring/tailoring_run.py`, `LlmPort` in
`infrastructure/llm/`, and `TailoringQueuePort` in `infrastructure/tasks/`. None of those modules is
imported here, and the dependency only ever points this way.

**These get no red-first cycle, and the reason is stated rather than left to look like an exemption**
(docs/sdlc.md §2, and the identical paragraph in `domain/posting/ports.py`). A `Protocol` has no
behaviour: every method body is `...`, so there is nothing that could fail an assertion, and a "test"
of one would either assert that Python still has ellipses or silently test whichever adapter it
imported to stand in. What proves these are right is that the adapters satisfy them — each carries an
`if TYPE_CHECKING:` structural-conformance assertion that makes `mypy --strict` do the checking — and
that the use cases compile against them. Verification here is by inspection, and for `LlmPort` the
thing to inspect is written out in its own docstring.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import ExtractedText
from tailorcraft.domain.posting.value_objects import JobPostingText
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoredDraft, TailoringRunId


class TailoringRunRepository(Protocol):
    """Persistence for the `TailoringRun` aggregate.

    Mostly the same shape as `BaseCvRepository` (`domain/intake/ports.py`) and
    `JobPostingRepository` (`domain/posting/ports.py`), including the `get`-raises /
    `find`-returns-`None` asymmetry both of those document. That asymmetry is deliberate and not an
    inconsistency to "fix": `get` is called where the caller already believes the row exists — a use
    case holding an id it minted, or one it is authorized to look up — so an absence there is
    exceptional and gets raised, while a lookup on whatever id happened to arrive is an ordinary
    branch and returns `None`.

    Two methods here have no counterpart in the earlier two repositories, and both exist because this
    is the first aggregate in the codebase that is written by one process and read by another:

    - `find`, for the worker (see its own docstring).
    - `save`, because this is also the first aggregate that is **loaded, mutated and persisted**. The
      earlier slices only ever built an aggregate and `add`ed it, so the question never came up; the
      house convention is silent rather than opposed. `ExecuteTailoringRun` calls `save` at three
      points (technical-plan.md steps 4, 6 and 7) and the alternative — relying on a mapper's
      identity map to notice the mutation at commit time — would make the moment state becomes
      durable invisible at the call site, which is the same objection that keeps the ownership check
      out of `get` below. Invisible rules are the ones a second entry point forgets.

    A third, `list_stale_running`, came later and for a third process: the beat sweep that records
    a run whose worker was lost (see its own docstring).
    """

    def next_identity(self) -> TailoringRunId:
        """Mint an id for a `TailoringRun` that does not exist yet.

        Synchronous, unlike everything else here: identity is application-assigned (UUIDv7,
        ADR-0007) and needs no I/O. That is what lets the aggregate be fully valid before it ever
        meets the database, which in turn is what makes the domain tests in `tests/unit/tailoring/`
        possible without one — and here it does a second job, because the id minted at request time
        is the *only* argument the queued task ever receives (ADR-0014 §5).
        """
        ...

    async def add(self, run: TailoringRun) -> None: ...

    async def save(self, run: TailoringRun) -> None:
        """Persist the current state of a run this repository already handed out.

        Says nothing about transactions. `save` means "make this the state you will hand back next
        time"; **when that becomes durable is the caller's boundary**, not this port's — which
        matters here more than anywhere else in the codebase, because `ExecuteTailoringRun` needs two
        commits rather than one (technical-plan.md, "Transaction boundaries"): `running` has to be
        visible to a polling client *while* a twelve-second call is still in flight, so step 4's save
        is committed on its own and step 6/7's save closes the second transaction.
        """
        ...

    async def get(self, run_id: TailoringRunId) -> TailoringRun:
        """Raises `TailoringRunNotFound` if no `TailoringRun` with this id exists.

        Does **not** check ownership. That is `TailoringRunNotOwnedBySession`, a use-case decision
        (G-29/AC-14) — a repository that silently filtered by session would make the authorization
        rule invisible at the call site, and invisible rules are the ones a second entry point
        forgets. This is the API's read path: something asked for a specific run, so "there is no
        such run" is an exceptional answer.
        """
        ...

    async def find(self, run_id: TailoringRunId) -> TailoringRun | None:
        """Look up a run by id, returning `None` when there is none. **This is the worker's lookup.**

        The counterpart to `get` above, and the reason the asymmetry earns a second method in this
        context rather than a second call site for the first one: the task is handed a run id that
        may legitimately have stopped existing between the enqueue and the pickup. A guest session
        purged at the 24-hour mark (ADR-0006) cascades its runs away while a message for one of them
        is still sitting in Redis, and `task_acks_late=True` makes redelivery of an already-purged id
        real rather than theoretical (G-26).

        So absence is an **ordinary branch** here — the task returns `MISSING`, logs one line and
        stops — where it is an exception in the API's read path. Raising would turn a routine,
        expected outcome into an error the worker's retry machinery would take seriously, and a
        retried lookup of a row that is gone forever is a loop with no exit.
        """
        ...

    async def list_for_session(self, sid: GuestSessionId) -> Sequence[TailoringRun]:
        """Every `TailoringRun` owned by `sid`, newest first, for `GET /api/tailoring-runs`. An empty
        sequence when the session owns none — never an error; "you have tailored nothing yet" is an
        ordinary answer, and it is the first thing a new visitor's history says."""
        ...

    async def count_for_session(self, sid: GuestSessionId) -> int:
        """How many runs `sid` owns, for the `TooManyTailoringRuns` check.

        A separate method rather than `len(await list_for_session(sid))` so the SQL adapter can
        answer with `COUNT(*)`. The saving is larger here than it was for the earlier two caps: a run
        carries a tailored CV *and* a cover letter, so materializing twenty rows to measure how many
        there are would pull every document body of a session's whole history into memory to produce
        one integer.
        """
        ...

    async def find_active_for_session(self, sid: GuestSessionId) -> TailoringRun | None:
        """The session's one run in flight, or `None`.

        "Active" means a status that is **not terminal** — `QUEUED` or `RUNNING`, the two values
        `TailoringRunStatus` documents as non-terminal and the two a client's poller keeps polling
        through. `SUCCEEDED` and `FAILED` are decided exactly once and are never active again.

        Serves the at-most-one-active-run rule, which is **soft** by decision (ADR-0014 §4): it spans
        aggregates, so it lives in `RequestTailoringRun` rather than on `TailoringRun`, and two
        genuinely concurrent requests may both pass it and both create a run. That is accepted,
        exactly as slice 1.1's F-23 and 1.2's P-32 accepted the same shape. The rule's job is to stop
        a double-click from buying two paid calls and to make "reattach after a refresh" trivial —
        not to be a lock on the hot path.

        Returns the run rather than a bool for that second job: the 409 body carries the active run's
        id so the client can attach to the run already in flight instead of paying for another.
        """
        ...

    async def list_stale_running(
        self, started_before: datetime, limit: int
    ) -> Sequence[TailoringRun]:
        """`RUNNING` runs that no worker can still be working on, for `AbandonStaleTailoringRuns` —
        the beat sweep that records a run whose worker was lost (G-25').

        **Which runs.** Status `RUNNING`, and either `started_at < started_before` **or no
        `started_at` at all**. That is `TailoringRun.is_stale` expressed as a filter, fold included:
        the caller passes `now - stale_after`, and `started_at < now - stale_after` is the same claim
        as `now - started_at > stale_after`. The `None` half is not a state this codebase writes —
        `mark_started` sets both fields together — but a filter that left out a row the rule calls
        stale would hide it from the caller's re-check, and that row would stay `RUNNING` for ever.

        **Bounded, and in a total order.** At most `limit` runs, oldest `started_at` first, a run with
        no `started_at` counting as the oldest of all (`NULLS FIRST` in SQL terms, where an ascending
        sort would otherwise put it last), ties broken by id. The bound is the caller's decision: a
        backlog after an outage must not load every row in one tick. The total order is what makes the
        bound cut in the same place every time, since whole-second timestamps make ties ordinary.

        **A listed run may already be decided by the time the caller reaches it.** No lock is taken:
        a redelivered `ExecuteTailoringRun` can record the same run between this read and the
        caller's write (G-36). The caller re-checks `is_stale` on each run and owns that race; its
        docstring records why the outcome is benign.

        Says nothing about transactions, exactly as `save` does not.
        """
        ...


class LlmPort(Protocol):
    """Turn a base CV and a job posting into a tailored CV and a cover letter.

    **Read this signature for what is missing — that list is the specification** (ADR-0004). No
    Gemini. No model name, no API key, no base URL. No HTTP, no status code, no header. No JSON, no
    schema, no `response_mime_type`, no `GenerateContentResponse`, no `safety_ratings`, no
    `finish_reason`. No timeout, no retry count, no backoff, no temperature, no `max_output_tokens`.

    Every one of those is real and load-bearing, and every one of them lives in the adapter. If any
    of them appeared here it would be a rename rather than a port: the domain would hold opinions
    about a vendor's SDK, and swapping the provider would become a change to the business model.
    (Naming them in this paragraph is the point of the paragraph; naming one in a parameter or a
    return type is the violation.)

    What the domain cares about is exactly this: *given a CV's text and a posting's text, give me two
    documents and the metrics of the call that produced them, or a failure I have a name for.*

    The metrics are not incidental. They ride back inside `TailoredDraft` so the run can record what
    it cost — model, prompt version, token counts, duration — which is how the 15-second budget and
    the token spend become observable without anything logging a prompt or a completion.
    """

    async def tailor(self, cv: ExtractedText, posting: JobPostingText) -> TailoredDraft:
        """Produce one tailored draft.

        Raises a `TailoringFailed` subclass (`domain/tailoring/errors.py`) on **every** failure —
        never a bare exception from whatever SDK, transport or parser the adapter happens to use
        underneath. The adapter guarantees that **structurally**, with an `except Exception` floor
        beneath its specific translations, rather than with an allow-list of the vendor errors it
        happened to think of: an allow-list is a bet that you enumerated every way a library can fail
        on input a stranger chose, and that bet loses. The CV-extractor sweep recorded in CLAUDE.md
        found `KeyError`, `AttributeError`, `ValueError` and `LimitReachedError` escaping through
        exactly such a list, and ADR-0012 obligation 10 writes the same lesson down for the egress.
        `Exception`, never `BaseException` — `asyncio.CancelledError` must still cancel a run.

        Unlike `JobPostingFetcherPort.fetch`, whose failures propagate to the router (ADR-0013),
        **these are caught by `ExecuteTailoringRun` and recorded** as `run.mark_failed(exc.reason)`.
        That contrast is ADR-0014 §2's line — by the time this is called the user is waiting and we
        are about to pay on their behalf, so every outcome is a row — and an application test asserts
        the recording rather than the propagation, which is what turns "fixing" this into a red.
        """
        ...


class TailoringQueuePort(Protocol):
    """Hand a requested run to whatever will actually execute it.

    **This is the Constitution §4.2 `TaskQueuePort` role, made context-specific** — a small,
    deliberate deviation from that section's naming, recorded as OQ-6 and decided in ADR-0014 §8.

    A single generic `TaskQueuePort.enqueue(name: str, **kwargs)` would put the *broker's* vocabulary
    — task names and an untyped kwargs bag — into a file under `domain/`. That is precisely the "port
    that is a rename" failure: a Protocol that adds a layer of indirection while faithfully
    reproducing the vendor's concepts, and buying a type signature that says nothing.
    `enqueue(run_id)` says what the domain wants: *make this run happen, not necessarily now.*

    §4.2 names a **role**, not a class, and this port fills it. Slice 1.5 will define
    `ExportQueuePort` the same way; if a third appears with an identical signature, that is the
    moment to reconsider — not now, on the strength of two.
    """

    async def enqueue(self, run_id: TailoringRunId) -> None:
        """Publish the run for execution. The id is the only argument, which is what makes the task
        idempotent by construction: everything else it needs it reads back from the row, and the row
        was committed before this was called (commit-then-enqueue, ADR-0014 §5).

        Raises `TailoringNotQueued` when the broker refuses or is unreachable (G-14). That is a plain
        `DomainError` and deliberately **not** a `TailoringFailed`, because **nothing was spent**
        (ADR-0014 §2): no call was made, no tokens were bought, no model was asked anything.
        `TailoringFailed` is the vocabulary of a call that happened; this is the vocabulary of a call
        that never will. The row is a separate question from the exception — the run is already
        committed as `queued`, so the router records it `failed` / `not_queued` in a second
        transaction and answers 503 rather than leaving a run the client polls until it gives up.
        """
        ...
