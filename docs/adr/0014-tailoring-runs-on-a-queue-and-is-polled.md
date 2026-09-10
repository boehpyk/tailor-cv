# ADR-0014: Tailoring runs on a queue and is polled, and a run that spends money is always a row

- **Status:** Accepted
- **Date:** 2026-09-10
- **Relates to:** ADR-0004 (the LLM behind a port; *a failed run is a recorded state, never a 500
  with nothing on disk*), ADR-0005 (Celery + Redis — this ADR **extends** its rule for choosing what
  goes on a queue), ADR-0006 §2 (choose the crash window whose survivor is recoverable),
  ADR-0007 (persistence conventions — no composites), ADR-0013 (no row for a failed fetch — this ADR
  **reconciles** it with ADR-0004 rather than picking a side), ADR-0010 (the guest session cookie).
  Supersedes nothing. Deliberately deviates from **Constitution §4.2** (port naming) and
  **§4.3** (`TailoredDocument` as an aggregate); both deviations are recorded in §8 and §9 below.

## Context

Slice 1.3 is the first slice that **spends money on a stranger's behalf**. Everything before it was
our own CPU and our own disk: an upload that fails costs a rejected request, an extraction that fails
costs a worker thread. From here on, a request can cause a payment to Google, take twelve seconds,
and produce the single most sensitive artifact in the product — a person's employment history,
rewritten.

That changes which questions have to be answered before any code is written, and it changes the
answers. Four of them were put to the human gate on 2026-09-10 and all four were decided as
recommended. They are one question wearing four hats: **what is a `TailoringRun`, and how does one
come to exist?**

1. Is the tailoring computed inside the HTTP request, or queued and polled? (OQ-1)
2. Does a *rejected* request leave a row behind? (OQ-2)
3. Does `POST /api/tailoring-runs` mint a guest session, as 1.1 and 1.2 both do? (OQ-3)
4. May a session have more than one run in flight? (OQ-4)

Slices 1.4 (editing) and 1.5 (export) both consume these answers — 1.5's renders are the same shape
with a different port — so recording them in a spec that dies with the slice would mean deciding them
twice. That is what this ADR is for.

## Decision

### 1. The run is queued and polled, not computed inside the request

`POST /api/tailoring-runs` commits a `TailoringRun` in `queued`, publishes one Celery task keyed on
its id, and answers **202 Accepted** with a `Location` header. A worker executes it. The client polls
`GET /api/tailoring-runs/{id}` until the status is terminal.

The synchronous alternative is genuinely tempting and it is not obviously wrong: the budget is 15
seconds (Constitution §7), and 15 seconds is a request a browser will happily wait out. It would
delete about eight tasks' worth of machinery — the queue port, the task, the worker's composition
root, the poller hook — and six rows of the failure contract with it.

**It loses on one argument, and the argument is decisive: the result must outlive the request that
asked for it.** Consider a disconnect at second eleven of a twelve-second call. Synchronously, the
event loop discards the response, the money is spent, and *nothing is recorded anywhere* — which is
precisely the failure ADR-0004 exists to forbid ("a failed run is a recorded state of `TailoringRun`,
never a 500 with nothing on disk"). Queued, the worker is not attached to the socket: it finishes,
records the outcome, and the user's next poll — or their next page load — finds two documents waiting
for them. The user paid for those documents. Losing them because a train went into a tunnel is not an
acceptable answer.

Two smaller consequences fall out and are worth naming, because together they are why 1.4 and 1.5 get
this for free: the polled resource gives 1.4's workspace a **real** status to render rather than a
timer pretending to be progress, and it gives 1.5 a pattern to copy for exports, which are CPU-bound
and were always going to be queued.

#### The second axis this adds to ADR-0005's rule

ADR-0005 decides what goes on a queue by **cost**: TXT and Markdown render inline because they are
string manipulation; PDF and DOCX go to a worker because they are CPU-bound. By that rule alone a
tailoring run would stay in the request — it is almost entirely *waiting*, and it burns no CPU worth
protecting the event loop from.

So this ADR adds a second axis, and the full rule now reads:

> **Queue the work when it is CPU-bound, _or when its result must outlive the request that asked for
> it_.**

The two axes are independent. An export is queued for the first reason. A tailoring run is queued for
the second. Something could be queued for both. Nothing that satisfies neither belongs on a queue —
that is what stops this from becoming "queue everything", which trades one 500 for a class of bugs
that only appear under redelivery.

### 2. A run that spends money is always a row — and the line is not "1.1's shape vs 1.2's shape"

A reader arriving here from the last two slices finds an apparent contradiction. Slice 1.1 records a
failed extraction as `status = extraction_failed` on a `BaseCv` row. Slice 1.2 (ADR-0013) records a
failed fetch as **nothing at all** — an HTTP error and a paste fallback. Which one does 1.3 copy?

**Neither, because that was never the question.** There is one question, asked at one moment:

> **Was anything spent, and is there an artifact to own?**

- 1.1 says yes: bytes are on a volume, and the row is the receipt for them.
- 1.2 says no: a failed fetch holds nothing and cost nothing but a socket.
- 1.3 says **both, at two different moments**, and the moment is the enqueue.

**Before the enqueue nothing has been spent**, so every rejection — a 401, a 404, a 409, a 429, a 503
from the rate limiter — creates **no row**. There is nothing to own and nobody is waiting.

**After the enqueue the user is waiting and we are about to pay Google on their behalf**, so **every**
outcome is a row: `succeeded`, and equally `failed` with one of nine reasons. A run that reached a
worker and produced nothing still owns the fact that it happened, the money it cost, and the twelve
seconds of a person's afternoon.

That single line reconciles ADR-0004 with ADR-0013 instead of choosing between them, and it is why
`TailoringFailureReason` is **persisted** while `FetchFailureReason` is not.

The rejected alternative — a `rejected` status for requests that never got as far as the queue — adds
four rows to the state machine, a stream of rows for requests that cost nothing, and a table that
grows with the failure rate of a validation check. Rate-limit telemetry belongs in the rate limiter.

### 3. This `POST` mints no guest session

Slices 1.1 and 1.2 both mint a session on `POST` and 401 on `GET`. **This endpoint 401s on both**, and
that asymmetry with its two predecessors is deliberate rather than an oversight.

The reason is short: a tailoring run **references a base CV and a job posting**, and both can only
exist under an existing session. A freshly minted session owns nothing, so minting one would create a
row for a request that is about to 404 anyway — a row that then has to be purged. `POST` here is not
the front door; `POST /api/base-cvs` is, and it is where the session comes from.

The counter-argument is consistency, and it is real: three endpoints with two cookie behaviours is a
thing a reader must learn. It loses because the alternative is a database row per doomed request, and
because the rule that actually generalizes is not "POST mints" but **"the endpoint that can be a
user's first contact mints"**.

### 4. At most one active run per session, soft, in the use case

A session may hold at most one run in `queued` or `running`. A second request gets **409
`tailoring_already_running`**, and the body carries the active run's id so the client can attach to
that run instead of paying for a second one.

Three things about this rule are load-bearing:

- **It is not an invariant of `TailoringRun`.** It spans aggregates — it is a statement about a
  *session's* set of runs — so it lives in `RequestTailoringRun` with a comment saying why. An
  aggregate that reaches out to count its siblings is an aggregate that needs a repository, and that
  is the road to a domain layer that cannot be tested without a database.
- **It is soft.** Two genuinely concurrent requests may both pass the check and both create a run.
  That is accepted, exactly as slice 1.1's F-23 and 1.2's P-32 accepted the same shape for their own
  caps. The alternative is a unique partial index or a lock, which buys correctness against a
  double-click that the disabled button already prevents, at the cost of a lock on the hot path.
- **It is the cheapest guard against a double-click costing two paid calls**, and it is what makes
  "reattach after a refresh" trivial: there is only ever one run to reattach to.

The rate limit (10/hour/session) bounds the hour. It does not bound the double-click. They are
different quantities and both are needed.

### 5. Commit, then enqueue

The run row is committed **before** the task is published, and the alternative is not merely worse —
it is broken.

- **Enqueue then commit:** the worker is a separate process on the same Redis, and pickup latency is
  measured in milliseconds. It will *routinely* read a run id whose row is not committed yet. Every
  such task finds nothing, returns, and the run sits `queued` forever. That is not a crash window; it
  is a race that fires under normal load.
- **Commit then enqueue** (chosen): the window is a crash between two statements. Its survivor is a
  `queued` run with no task — visible in the database, visible to the user as "waiting", and
  recoverable by re-enqueuing.

This is ADR-0006 §2's rule applied unchanged: **choose the crash window whose survivor is
recoverable.**

If the enqueue itself raises — the broker is down — the router marks the run `failed` /
`not_queued` in a **second transaction** and answers 503. If that second write also fails too, the run
stays `queued`, which is the recoverable survivor again.

This is also why `mark_failed` is legal from `queued` as well as from `running`. A run can fail
before it ever starts, twice over: a failed enqueue, and a redelivery that arrives after the run went
stale. Neither may be recorded by pretending the run started.

### 6. One retry mechanism, in the adapter, never Celery `autoretry_for`

The Celery task declares **no** `autoretry_for`, **no** `retry_backoff` and **no** `max_retries`, and
a test asserts that rather than trusting this paragraph.

Retrying is a decision that requires knowing **what kind of failure this was**: a 429 is worth
retrying, a safety refusal is a refusal repeated at twice the price, and an input that does not fit
will not fit the second time either. Only the adapter has that information — by the time an exception
reaches Celery it is an opaque failure, and Celery's answer to an opaque failure is to run the whole
task again.

Two retry layers do not add; they **multiply**. A bounded cost (2 attempts) under an unbounded one
(Celery's default `max_retries` is 3) is 8 paid calls for one button press, and every one of them
after the first re-enters a run the aggregate has already recorded as failed. Concretely, the bound
is: at most `llm_max_attempts` (2) attempts, only for `LlmUnavailable`, `LlmRateLimited`,
`LlmTimedOut` and `LlmOutputInvalid`, all of it under one outer `asyncio.wait_for` of
`llm_total_deadline_seconds`.

Idempotency is what makes a redelivery safe, and it is delivered by the **aggregate**, not by a flag
in the task: `mark_started` is legal only from `queued`, so a redelivered task cannot make a second
call; `mark_succeeded` / `mark_failed` are legal only once, so a redelivery cannot overwrite a
recorded outcome. `task_acks_late = True` makes redelivery real rather than theoretical, which is
exactly why this is a domain property and not a comment.

### 7. The Celery result backend must never carry a document

The task takes **one string** — the run id — declares `ignore_result=True`, and **returns `None`**.

This is a privacy control, not a style preference. `result_expires = 3600` means anything a task
returns sits in Redis for an hour: outside Postgres, outside the 1.6 purge's reach, outside every
retention promise this product makes, in a datastore with no backup policy and no column anyone
audits. A tailored CV in the result backend would be a second copy of a stranger's employment history
in a system nobody thinks of as a database.

The argument's mirror image is why the **argument** is a UUID string: `sentry-sdk`'s Celery
integration captures task arguments, so the task's signature is a log field set. A UUID is safe. A
CV would not be.

### 8. `TailoringQueuePort`, not the Constitution's `TaskQueuePort`

Constitution §4.2 names a `TaskQueuePort`. This slice defines `TailoringQueuePort` with a single
method, `async def enqueue(self, run_id: TailoringRunId) -> None`.

A generic `TaskQueuePort.enqueue(name: str, **kwargs)` would put the **broker's** vocabulary — task
names and an untyped kwargs bag — into a file under `domain/`. That is the "port that is a rename"
failure mode: a Protocol that adds a layer of indirection while faithfully reproducing the vendor's
concepts. `enqueue(run_id)` says what the domain actually wants: *make this run happen, not
necessarily now.*

§4.2 names a **role**, not a class, and this port fills that role. Slice 1.5 will define
`ExportQueuePort` the same way, and if a third one appears with an identical signature that is the
moment to reconsider — not now, on the strength of two.

### 9. `TailoredDocument` is a value object on the run, not the second aggregate §4.3 names

Constitution §4.3 lists the `tailoring` context as owning `TailoringRun` **and** `TailoredDocument`.
In this slice the tailored CV and the cover letter are **value objects held by the run**
(`TailoredCv`, `CoverLetter`, paired as `TailoredDocuments`), stored as two `TEXT` columns on
`tailoring_run`.

- They are **always created together, always read together, always deleted together**. A consistency
  boundary that nothing ever crosses independently is not a boundary; it is a join.
- The invariants that matter — *`succeeded` iff both documents exist*, *a half result is a failure* —
  become **cross-object** invariants the moment the documents live elsewhere, and a cross-aggregate
  invariant is exactly the thing aggregates exist to avoid.
- A second table would need its own repository, its own identity, its own migration and its own
  authorization rule, for a row that never has a different owner, lifetime or access rule from its
  run.

**The named trigger that would change this**, stated now so the change is a decision rather than a
drift: **the day a document gets an independent lifecycle.** Versioning (1.4's editor keeping an edit
history), an export addressed per-document rather than per-run, or a document re-used across runs.
If 1.4 introduces revisions, that is the moment — and this paragraph is the note saying it was
foreseen.

Until then, the Constitution's `TailoredDocument` is a *concept in the ubiquitous language* realized
as a value object, which is the ordinary relationship between a language and a model.

## Alternatives

- **Synchronous tailoring inside the request.** Rejected: §1. The recorded cost of the rejection is
  real — roughly eight tasks of machinery, six failure rows, the `queued` status and the whole
  idempotency requirement. What it buys back is durability of a paid result, and 1.4's honest status.
- **Server-Sent Events or WebSockets instead of polling.** Rejected for this slice: it trades a
  simple pollable state machine for a second transport, a second reconnection story and a second
  thing nginx must be configured not to buffer, in exchange for saving a handful of one-second polls
  on a call that lasts twelve seconds. Revisit when there is something to stream *to* — 1.4's editor
  accepting partial output would be the reason.
- **Streaming the model's output.** Rejected for the same reason and one more: the UI has nothing
  useful to do with half a CV, and re-validation (ADR-0004) is a statement about a *complete*
  document.
- **A `rejected` status for requests that never reached the queue.** Rejected: §2.
- **Celery `autoretry_for` instead of, or in addition to, the adapter's retry.** Rejected: §6. "In
  addition to" is the dangerous one, because it looks like defence in depth and is actually
  multiplication.
- **A unique partial index enforcing one active run per session.** Rejected: §4. It buys correctness
  against a race the disabled button already prevents, at the price of a write-time lock on the hot
  path, and it turns a friendly 409 into an integrity error that has to be translated back into one.
- **Both `base_cv_id` and `job_posting_id` as real foreign keys.** Rejected: all three tables already
  cascade from `identity_guest_session`, so an FK adds a write-time check and a second cascade path
  for a guarantee the session FK already gives — and it makes two bounded contexts one schema. If
  Phase 2 ever lets a user delete a single base CV while keeping the session, the answer is a
  nullable reference plus a "the source CV was deleted" state, not a cascade that silently erases
  history.

## Consequences

- **There is a second composition root.** `infrastructure/tasks/container.py` builds the worker's
  object graph; `infrastructure/api/deps.py` builds FastAPI's. Reusing `deps.py` in a worker would
  mean faking a `Request`. **A port with no binding is a bug** now has to be checked against *both*
  files, and the checklist grows by one every time a port is added.
- **The worker gets an async engine created inside the loop `asyncio.run` opens.** An asyncpg
  connection is bound to the loop it was created on; a module-level engine produces
  `RuntimeError: got Future attached to a different loop` from a worker that looked fine with one
  task. This is the same footgun `conftest.py` already documents, met in a second place.
- **`/health/ready`'s Celery probe stops being a nicety.** With the work on a queue, a stopped worker
  looks *exactly* like a healthy system: the API answers, the database answers, and every run queues
  forever. The probe is the only signal, and G-15's "Waiting for a worker…" is the only thing the
  user sees.
- **A named queue (`tailoring`) exists from day one.** One line now; expensive to retrofit once 1.5's
  renders share the worker, where a long PDF render would otherwise starve a tailoring run. A task
  published to `tailoring` while the worker listens only to `celery` is a run that queues forever and
  looks, again, exactly like a healthy system — so the deploy verifies the worker's queue list.
- **`make db.dump` now contains tailored CVs and cover letters.** The same PII handling as the
  uploads, and the same reminder that a dump is not a backup of this product.
- **A run that retries misses the 15-second budget, and that is accepted rather than hidden.** One
  retry of a twelve-second attempt plus a second of backoff is twenty-five seconds, which is why
  `llm_total_deadline_seconds` is 25 — set *above* the budget on purpose, so a retry can complete
  rather than being cut off with nothing to show for two paid calls. The budget is a target for the
  happy path, not an SLA over the retry path.
- **The accepted residual, named rather than pretended away:** if Postgres is unavailable inside the
  worker at the moment a successful outcome is recorded, **the documents are lost and the money is
  spent**. Persisting the draft "first" is the same single write, so no ordering buys it back. The
  run is eventually marked `abandoned` by a redelivery past the stale window, and the user is told it
  was interrupted.
- **This ADR is consumed by 1.4 and 1.5, so changing it is not a local edit.** 1.4 renders the polled
  status; 1.5 copies the queue-port shape, the commit-then-enqueue ordering and the
  no-document-in-the-result-backend rule verbatim.
