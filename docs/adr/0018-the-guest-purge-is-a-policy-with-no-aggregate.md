# ADR-0018: The guest purge is a policy with no aggregate, and its schedule ships off until rehearsed

- **Status:** Accepted
- **Date:** 2026-09-19

## Context

ADR-0006 set the retention policy — guest data lives at most 24 hours, the predicate is `expires_at`
and nothing else, rows are deleted before files — and left the mechanism to the slice that would
build it. That slice is 1.6, and it is the first code in the `retention` bounded context and the
**first `DELETE` in this codebase**.

Four forces meet here.

1. **A new bounded context with no aggregate looks like a slice that got lazy.** Constitution §4's
   table already says `retention` owns *"the guest 1-day purge (no aggregate — a policy + a port
   method)"*, but a row in a table is not an argument, and every other context in this codebase
   earned an aggregate in its first slice.
2. **The job's only failure mode is silence.** Beat stopping raises nothing, logs nothing, and is
   invisible to `/health/ready` — `control.ping` reaches workers, and beat is not a worker. A purge
   that never runs looks exactly like a purge with nothing to do.
3. **Two irreversible systems, no rollback.** A purge deletes rows *and* unlinks files. `make
   db.dump` covers the first half only, and restoring either half alone is not a rollback. The
   roadmap therefore carries a deferred item: the schedule stays off until the job has been
   rehearsed by hand, on real data.
4. **Two mechanisms in this slice fail in opposite directions**, and a reader who meets them in the
   wrong order will "fix" one of them: the purge lock **fails open**, and the orphan sweep's
   database cross-check **fails closed**.

## Decision

**1. `retention` gets a context, two use cases, two ports, three value objects — and deliberately no
aggregate.** An aggregate exists to protect an invariant across a transaction boundary. A purge run
has none: there is no rule about a run that a second run could violate, no state transition anyone
can get wrong, and no question the business asks of a past run. A `PurgeRun` row would be an **audit
table of deletions in a product whose premise is deleting things on a timer** — it would need its own
retention policy. The one invariant the purge leans on (`expires_at > created_at`, and the single
question `is_expired`) already belongs to `GuestSession` in `identity`, where ADR-0006 §1 put it.
`retention` **applies** that predicate; it does not restate it.

**2. Rows first, committed, then files — per session, not per batch.** ADR-0006 §2 chose the crash
window; this ADR names the transaction boundary that makes the sentence true. One committed
transaction per session, each contained in a `SAVEPOINT`, so one refused `DELETE` neither aborts the
batch nor expires the identity map holding the remaining candidates. A single transaction over the
batch would make a mid-batch failure un-partial, and would unlink the files of sessions whose rows
then came back.

**3. The file keys are collected *before* the delete, and an export's key is derived rather than
read.** After the delete they are unrecoverable. A `rendering` or `failed` export job can have
written bytes while carrying `file_key IS NULL`, so the key comes from `(id, format)` through
`FileRef.for_export` — ADR-0016's determinism guarantee, consumed here for the first time.

**4. The signal to trust is the backlog, not the log line and not the heartbeat.** `overdue` is a
count of expired sessions, computed from the same predicate the purge uses. A run row, a log line and
a heartbeat can all be written by a job that is not working; `overdue` cannot be faked by one.

**5. The schedule ships off.** `GUEST_PURGE_ENABLED` defaults to `false`, in `Settings` and in
`.env.example`, and `create_celery` adds the beat entry **only** when it is true. The flag is flipped
inside slice 1.6, as the last step of an ordered rehearsal on real data: dry run → `--limit 50` →
confirm `overdue` fell by 50 → full run → read `jobs.guest_purge`. `scheduled: false` is published on
`/health/ready` and rendered in the UI **in plain words**, so a flag that stays false for ever is
visible rather than filed.

**6. The lock fails open; the cross-check fails closed. Both directions are deliberate.** The purge
lock is advisory: Redis down must not stop a purge, because the cost of a skipped purge is a broken
privacy promise and the cost of an overlap is duplicated work on an idempotent job. The orphan
sweep's database cross-check is the opposite: **deleting a file because we could not ask whether it
is referenced is the one irreversible mistake this tool can make**, so it deletes nothing and exits
non-zero. Put a mechanism's failure on the side whose loss is recoverable — which is *duplicated
work* in one case and *a file* in the other.

**7. The lock TTL is a derived constant, not a setting** — `PURGE_LOCK_TTL_SECONDS =
TASK_TIME_LIMIT_SECONDS + 60`. A setting would need a startup guard refusing a TTL at or below the
hard time limit, which would be the **fourth** startup refusal that does not exit the container under
`uvicorn --workers N` — a known, measured, still-open bug carried since slice 1.3. A constant derived
from the very limit it must exceed cannot be misconfigured. **The best guard is the one you do not
need.**

**8. The heartbeat and the lock are infrastructure, not domain ports.** They are facts about how this
job is *operated* — once at a time, observably — not about what retention *means*. The use case runs
identically with or without them; the entry point owns both. This follows the precedent
`infrastructure/rate_limit.py` set and states in its own docstring.

**9. No new queue, no new migration, no domain event.** The purge rides the default `celery` queue,
for the reason both existing sweeps do: recovery and hygiene must not queue behind the workload they
clean up after. Every cascade, every `guest_session_id` index and
`ix_identity_guest_session_expires_at` already exist, written in earlier slices with comments naming
this one — and the slice proves that with a schema test rather than asserting it. A
`GuestDataPurged` event would reach every listener and every log line, which is the one place
Constitution §8 says a CV-adjacent payload must never go, and its only listener would be the logger
the entry point already is.

## Alternatives

- **A `PurgeRun` aggregate with a persisted history.** Tempting: it gives the run an identity, puts
  the counts somewhere better than a log line, and lets `/health/ready` read the last run from
  Postgres (which survives a Redis flush). Rejected: it protects no invariant, it is an audit table
  that needs its own retention policy, and it would invert the trust model — an operator told to
  trust a *run row* is trusting something a broken job can still write. `overdue` is the honest
  signal, and it needs no table.
- **A `purge` method on `GuestSessionRepository`.** Rejected: that port is `identity`'s, speaks about
  one session at a time in the language of authentication, and three of its four methods are about
  resolving a cookie. Retention's questions — *how much is overdue?*, *give me the next batch with
  their file keys* — are a different consumer's language. ADR-0016 already declined to add
  speculative methods for 1.6 on exactly this reasoning: **1.6 adds the one it needs.**
- **Listing on `FileStorePort`.** Rejected: that port is `put`/`get`/`delete`, a key-value store.
  Bolting listing on would oblige a future S3 adapter to implement pagination for every caller of a
  port that never needed it. The orphan scanner is its own port, and it is the one place ADR-0011
  §4's *"the layout is sweepable without the database"* is actually used.
- **Ship the schedule on and rely on the operator to have rehearsed.** Rejected: it inverts the
  roadmap's own deferred item, and the first automated run of an irreversible two-system `DELETE`
  would be the rehearsal.
- **Delete orphans on age alone**, as ADR-0011 §4's "no query at all" appears to license. Rejected:
  §4's claim is about the *layout* — a UUIDv7 filename carries its own timestamp, which is what makes
  the **scan** database-free. It is not a licence to **delete** on age. Phase 2.2 will put files on
  this volume that are older than any window and must live for ever. The scan is database-free; the
  deletion is not.
- **A single transaction for the whole batch.** Rejected: see decision 2.
- **A fourth startup guard for the lock TTL.** Rejected: see decision 7. If it is ever wanted, it is
  named in CLAUDE.md beside the other three in the same commit.

## Consequences

- **A crash between the committed row delete and the unlink leaves an orphaned file, by design.**
  That survivor is invisible to every row-driven query, so the recovery has to be directory-driven:
  `purge-guests --orphans`, operator-run only, never on beat, with a mandatory cross-check. It is the
  only recovery for an unlink that failed, a SIGKILL mid-session, and a worker that wrote a rendered
  file after its row was purged.
- **A purge has no rollback.** Before the first real run the operator takes `make db.dump` **and** a
  snapshot of the uploads volume, and understands that restoring either half alone is not a rollback:
  rows without files is a broken app with a green restore, files without rows resurrects exactly what
  was deliberately unlinked.
- **`scheduled: false` must stay visible.** The flag is the one thing standing between "we delete
  guest CVs after 24 hours" being true and being marketing. The UI says so in words, the roadmap's
  deferred item tracks it, and the slice is not complete until the flip has happened.
- **Two concurrent purges are safe and may double-count.** The lock is advisory, so the loser's
  `DELETE` affects zero rows and its unlinks are `missing_ok`. Both runs exit 0; their reported counts
  may overlap. Accepted, and the reason the lock is not a correctness mechanism. *(Tightened by the
  amendment below: only the run whose `DELETE` removed a session reports it.)*
- **`files_unlinked` means *keys we asked the store to remove*, not *files that existed*.**
  `FileStorePort.delete` is `missing_ok` by contract, so the number cannot carry the stronger claim.
  Named here so nobody reads it as the stronger one.
- **When Phase 2.2 introduces accounts**, `guest_session_id` becomes nullable and the ownership
  predicate (`NULL` ⇒ owned by a user ⇒ never selected) becomes expressible. A tripwire test in this
  slice asserts the four columns are `NOT NULL` **today**, and goes red at exactly the moment the
  obligation ADR-0006 §3 named becomes testable. That is the slice that writes the real test.
- **`StartGuestSession` still takes `retention_hours: int`** while the purge takes a
  `RetentionWindow`. One promise, two types, for one more slice. The obvious follow-up is to give
  `identity` the value object too; it was not done here because this slice touches enough.

## Amendment: 2026-10-01, from the plan of slice 2.4 (`workspace-registration-cta`)

Slice 2.4 adds the claim (ADR-0025): a guest session's rows are re-keyed to a user, and the session row
is deleted, in one transaction. It is the first thing in this codebase that changes a row's owner after
creation, and it breaks an assumption decision 3 rested on without saying so. Decisions 1–9 stand.

**(a) `delete_session` returns whether it deleted the session, and the purge unlinks a session's keys
only when it did.**

- **The race.** Per session the purge reads the session's file keys (decision 3, no lock), deletes the
  session (one committed `DELETE`; the cascade), then unlinks the keys. Until 2.4 the keys could not
  stop being the session's between the read and the delete, because rows never changed owner. If a
  claim of that session commits in between, the purge's `DELETE` finds nothing to cascade — the rows
  are a user's now — and the purge would then **unlink files a user owns**: rows pointing at nothing,
  the outcome ADR-0006 §2 chose its crash window to avoid. The window is narrow (the purge selects
  `expires_at <= now`, the claim `expires_at > now`, so a claim must straddle the expiry instant), but
  a timing argument is not a lock.
- **The fix is one boolean.** `ExpiredGuestDataPort.delete_session` returns `True` iff **this call's**
  `DELETE` removed the row, and `PurgeExpiredGuestSessions` skips the unlinks of a session whose
  delete returned `False`. Because the claim deletes the session row in its own transaction, *"my
  `DELETE` removed the session"* holds exactly when no claim of it committed first; and a claim cannot
  commit afterwards, because it locks the session row `FOR UPDATE` before re-keying anything and the
  purge's `DELETE` holds that row. **Unlink only what you deleted**: the rows deleted and the files
  unlinked are one set, which is 2.3's `DELETE … RETURNING` lesson (ADR-0023 decision 5) one level up.
- **The port's old contract is reversed on purpose.** Its docstring said the method *returns nothing*,
  because a count of deleted rows would be a number the domain could only misuse. This is not a count:
  it is the one fact that tells the use case whether the keys it holds still belong to what it
  deleted. Deleting an already-deleted session stays a no-op, not an error; it now also says so.
- **Decision 3 stands.** Keys are still collected before the delete, and export keys still derived.
  The new rule is what makes that order safe once a row can change owner between the two steps.
  Re-collecting the keys under a lock inside `delete_session` was considered: correct too, but it moves
  the key collection and its derivation rule into the delete path and changes the port more. A refusal
  margin before expiry on the claim side was rejected as a timer standing in for a lock.
- **`PurgeReport` gains `sessions_skipped`**, counted when a delete returned `False`, so an operator
  can see the race happen; it should read approximately zero. It also tightens the Consequence on
  concurrent purges: the loser of two concurrent purges now skips the session rather than reporting it
  a second time.
- **The two-version deploy window.** A 2.3 worker's purge ignores the return value (its port returns
  nothing), so an old purge running beside a new API would be the unguarded one. The release stops
  `worker` and `beat` across the deploy and starts them on the new image, so that window is zero by
  the release order; 2.4 confirms this in the release script rather than assuming it.

## Amendment: 2026-10-02, from the plan of slice 2.5 (`identity-email-verification`)

Slice 2.5 adds rows that exist to expire — pending registrations (24 h) and password resets (60 min) —
and ADR-0020's expired logins have been accumulating since 2.1. Retention gains its **second timer
job**. Decisions 1–9 and amendment (a) stand.

**(b) The identity token sweep is retention's second timer job, and it ships on.**

- **What it deletes.** Expired pending registrations, expired password resets, and expired logins
  (their retired refresh-token hashes by cascade; ADR-0020's amendment (a)). The predicate is
  `expires_at <= now`, inclusive — the rule each aggregate already applies on sight. Nothing else: no
  user, no saved CV, no history, no unexpired token. It deletes rows only; there are no files, so
  decision 2's *rows first, then files* has nothing to order.
- **Where it lives, and why there.** `retention` owns *deleting what has aged out*. The use case is
  `PurgeExpiredIdentityTokens`, with its own port, `ExpiredIdentityTokenPort` (`count_overdue` and one
  `delete_expired_*` per kind, each taking a limit), so retention's domain names no identity type
  beyond ids and counts — the allow-list test from 2.3 holds it there. It returns an
  `IdentityTokenSweepReport` with three counts. No aggregate and no event, for decision 1's and
  decision 9's reasons.
- **How it runs.** Hourly on beat, on the default `celery` queue (decision 9), in batches of 1 000
  rows per table per statement until a batch deletes nothing, so no single statement holds locks on a
  large backlog. It takes no user lock (ADR-0028's lock order); a reset that a completion has locked
  makes the sweep wait, and one the completion deleted first leaves it nothing to do. The entry point
  logs one line with the three counts and a duration; a failure logs its exception type and the next
  tick retries.
- **On by default — no flag, no rehearsal — unlike decision 5.** The purge ships off because it
  deletes **a person's work**, in two systems, with no rollback; the first automated run must not be
  the rehearsal. This sweep deletes only rows that **every code path already refuses to use**: an
  expired confirmation or reset link is refused and deleted when presented, and an expired login is
  deleted by `RefreshLogin` on sight. The sweep therefore changes *when* those rows go, never *whether
  a user can still use them*. There is nothing a hand rehearsal could reveal that a test cannot prove:
  a test with expired and unexpired rows of each kind beside a user's saved CV, history and live
  logins asserts it deletes exactly the expired ones, with a named mutation on the boundary. A flag
  that defaults off would, by decision 5's own argument, need a visible `scheduled: false` and a flip
  owed in the slice — machinery for a risk this job does not carry.
- **No lock and no heartbeat.** Two overlapping sweeps are harmless: a `DELETE` of an already-deleted
  row affects nothing, and their counts may overlap (decision 6's reasoning, without the lock it adds
  for the purge). The signal to trust is again the **backlog** (decision 4): `/health/ready` reports
  how many rows expired more than two intervals ago (ADR-0019's amendment (a)).
- **Constitution §4's `retention` row** says *"the guest 1-day purge on a timer"*; the identity token
  sweep is a second timer of the same kind, and the row's wording, which names what retention owns
  rather than listing its jobs, needs no change.
