# ADR-0042: A credit is a user's: an append-only history whose sum is the balance, charged with the run's request and refunded with its failure, in the same transaction

- **Status:** Accepted
- **Date:** 2026-11-07
- **Relates to:** ADR-0014 (**amended (f)** the same day: a guest `POST` is a 403, one active run per
  account is now hard, queued-run recovery, a refund at every failure site, the worker's full rollback),
  ADR-0023 (**amended**: a guest no longer tailors or exports), ADR-0006 (**amended (i)**: the billing
  rows' retention), ADR-0026 (**amended**: the one opt-in payment notice whose consent lives here),
  ADR-0043 (the counters this design increments), ADR-0029 (the user-only shape copied here), ADR-0007
  (persistence), ADR-0027 (a `User` exists only once confirmed, which is where an account opens),
  ADR-0032 (`grant-credits` follows `grant-role`). ADR-0022 and ADR-0025 are **not** amended: billing
  rows are user-only and the claim moves none. Supersedes nothing. The number 0039 is still held for a
  backups decision, so its absence on disk is intended.

## Context

Slice 5.2 makes tailoring cost something: **one credit per run**, the cover letter and the before/after
score included, edits and exports free. A failed run gives its credit back. Every account gets one free
credit, existing accounts included, and guest tailoring and guest export close in the same release. Six
forces meet:

1. **It is money-shaped.** 6.1 adds purchases to the same history. A balance that drifts from what
   happened, or a run charged twice, is a support ticket about money.
2. **The charge and the run must agree.** A run with no charge is free work; a charge with no run is a
   stolen credit. The owner's rule (D-2) is that charge and refund happen **in the same transaction as
   the run's state change**.
3. **A run fails in five places**: the worker's `_record_failure`, the stale sweep, the handler's refused
   enqueue, and (new here) the recovery of a `queued` run whose task was lost. Each needs the refund.
4. **Double submits are real.** Two tabs, a double click. ADR-0014 §4 made one-active-run soft, which is
   fine when a duplicate run costs us a cent and wrong when it costs the user a credit.
5. **The deploy runs two versions** (`deploy.yml`: the new API starts, then the migration runs). The old
   API's last requests create **uncharged** runs that the new worker may fail.
6. **The worker commits on every save**, and a version conflict rolls back only its SAVEPOINT. A refund
   written before that save would survive the conflict and commit with the next run.

## Decision

**1. A new context, `billing`, with one aggregate, `CreditAccount`.** Not `identity`: a credit is not an
account fact but a ledger with invariants of its own, and 6.1 grows it. Not `tailoring`: a run knows what
it cost us (`LlmCallMetrics`, unchanged), never what it cost the user. The two meet in the **application
layer** only: `RequestTailoringRun` composes billing's `ChargeCreditForRun` the way it composes
`GetBaseCv`, on one session. `domain/billing` imports `domain/shared` and `identity`'s `UserId` (the
kernel) and nothing else; it names a run by its own `ChargedRunRef(UUID)`, as scoring names a CV.

**Billing rows are user-only**, the ADR-0029 shape: no guest owner column, so ADR-0022's owned-table
catalogue and ADR-0025's claim are untouched. A claimed guest run moves uncharged, and no credit moves.

**2. The balance is the sum of the entries, never a stored column.** An entry is `welcome` or
`existing_account` (the opening grant, +1), `grant` (+1..100, operator), `charge` (−1) or `refund` (+1);
a run id is set iff the kind is `charge` or `refund`. Entries are never updated or deleted (erasure's
cascade excepted); the aggregate has no method that could, and a test captures the SQL. The aggregate
loads **every** entry, so "never below zero" and "a refund needs a charge" are checks in memory. A history
is small: at most 500 runs per user, so about a thousand entries. *`ponytail:` the whole history loads per
charge; add a stored balance checked against `SUM` the day a history passes ~5,000 entries.*

The database holds the concurrency half: two partial unique indexes (one `charge` per run, one `refund`
per run), one for the single opening grant per user, and one row lock.

**3. The charge: in the request's transaction, after the run's insert, under one lock.** For an account,
after the existing ownership, active-run and cap checks and `runs.add(run)`:

```text
lock_for_charge(user)   SELECT … FROM billing_credit_account … FOR NO KEY UPDATE, then the entries
account.charge(run)     NoCreditsLeft at 0 (409 no_credits) · RunAlreadyCharged if charged
save                    insert the −1 entry
re-check                find_active_for_owner(owner, excluding=run.id) → 409 tailoring_already_running
publish                 the run's events, then the account's
```

The handler commits only after the use case returns (ADR-0014 §5), so **the charge commits with the
`queued` row or not at all**. `FOR NO KEY UPDATE` is what `with_for_update(key_share=True)` renders, and
here that is the lock wanted: it conflicts with another charge and with erasure's delete, and not with
the `FOR KEY SHARE` a refund's foreign key check takes. The test compiles the statement and pins the SQL.

**4. One active run per account is now hard, as a side effect of the lock.** The re-check in decision 3
runs while this transaction holds the account lock, so a second request that waited on the first's lock
sees the first's committed run (READ COMMITTED takes a new snapshot per statement) and refuses. That is a
lock on the hot path ADR-0014 §4 rejected; it is per account, not global, and money is the new force. It
is named here so nobody "relaxes" it: removing the lock or moving the re-check before it brings back
double charges. The rule stays in `tailoring`; billing never imports it.

**5. The refund: in the failure's transaction, with no lock, and only for a charged run.** Refunds only
add credit, so they cannot break "never negative" and need no serialization with charges. "At most once"
is the aggregate's rule, backed by the refund index. **A refund needs a charge**: failing an uncharged run
writes nothing and is not an error (`refund` returns `False`). That one rule makes the deploy window safe
(an old API's uncharged run fails for free) and covers a guest run made before release, claimed or not.

**One collaborator for every failure site**, `RecordRunFailure`: mark failed, refund (a guest owner is a
no-op), count `runs_failed` (ADR-0043), save, publish. The worker, the sweep and a new
`RecordRunNotQueued` use case (replacing the handler's body, so no router orchestrates billing) all go
through it. The sites used to repeat *mark, save, publish* on purpose; with money added, three copies are
three places for the rule to drift.

**6. A failed save in the worker rolls back the whole transaction.** `CommittingTailoringRunRepository.save`
keeps its SAVEPOINT (a failed flush must not expire the map) and now calls `session.rollback()` before
re-raising, so a refund written before the save can never outlive a lost race. The stale sweep therefore
**lists ids first** and re-reads each run with `find` (instances are expired after a rollback and an async
session refuses a lazy refresh), re-checks staleness, records the failure, and on a conflict counts it and
carries on. On the API path the handler's single commit already covers run, refund and counter.

**7. A `queued` run whose task was lost is recovered.** `TailoringRun.is_lost_in_queue(now, after)` is
strict, like `is_stale`. The sweep lists both stale `running` runs (→ `abandoned`) and runs still `queued`
30 minutes after `requested_at` (→ `not_queued`, an existing retryable reason with existing copy), each
bounded by the same limit, and refunds them through decision 5. A late delivery finds a decided run and
SKIPs. The 30 minutes is a setting, refused at startup in every environment unless it is above the
broker's `visibility_timeout` (600 s), so a redelivery always gets its chance first; `check-settings`
gains the refusal.

**8. An account opens eagerly, with a backfill.** `ConfirmRegistration` composes `OpenCreditAccount` after
`users.add(user)` on the same session, so the user and their `welcome` entry commit together. The
release's migration opens one account with an `existing_account` entry per existing user, idempotently,
and a catalogue test keeps *every user has exactly one account* true. In the deploy window the new API's
confirm answers 503 until the migration lands; the pending registration survives the rollback, so the
same link works seconds later.

**9. The guest boundary is a 403, on the paths that exist.** `POST /api/tailoring-runs` answers **403
`account_required`** from a handler with no body parameter and no dependencies, so nothing is validated,
resolved, rate-limited or minted first. `routers/export.py` (guest-only) refuses every route through one
router-level dependency, before path validation. The paths stay because a stale bundle, cached for up to
24 hours, should get a clear 403 and not a 405. Guest `GET` and `PUT` on runs stay: a run made before
release is readable, editable and claimable until the purge.

**10. Grants are CLI only, like roles.** `grant-credits --user-id <uuid> --credits N [--dry-run]`, N in
1..100, under the same lock as a charge, with `erase-account`'s guards and exit codes. No HTTP writer and
no revocation. A succeeded run keeps its credit even if its output is wrong (an invented skill the
validator cannot see); the operator's remedy is a grant.

**11. Packs and the payment notice are facts on the account.** `pack_3` (3 credits, $5) and `pack_10`
(10, $12) are a closed set in code, as ADR-0030's layouts are. A pack click charges nothing and records
the account's first click; consent to one payment notice is a nullable instant, sent by nobody in this
slice (ADR-0026 as amended).

## Alternatives

- **A stored balance column, updated with each entry.** One read instead of a `selectin`, and a cache that
  can drift from the history it summarises. Rejected for now: decision 2 names the trigger.
- **Credits as a column on `identity_user`.** Smallest diff. It loses the history 6.1 needs, and puts a
  ledger's invariants in the kernel every context imports. Rejected.
- **Credits inside `tailoring`.** One context fewer, and a run that knows what it cost the user. Rejected:
  decision 1.
- **Lazy opening on first touch.** Leaves `identity` untouched, but turns a `GET` into a write, needs a
  get-or-create race handler on every billing path, and loses the instant of the free grant. Rejected.
- **Lock the account for refunds too.** Simpler to reason about, and it adds lock traffic to a write that
  cannot break the invariant it would protect. Rejected: decision 5.
- **A unique partial index for one active run per user.** ADR-0014 §4 rejected it, and it would be a
  second mechanism beside a lock that already gives the rule. Rejected.
- **Delete the guest routes.** Cleaner, and a stale bundle gets a 405 it cannot explain. Rejected:
  decision 9.

## Consequences

- **The hard re-check depends on READ COMMITTED.** A test stages the second request blocked on the lock,
  and a mutation (re-check before the lock) must turn it red.
- **Erasure can deadlock with a refund.** Erasure locks the user and cascades; a refund's foreign key check
  takes `FOR KEY SHARE`. Postgres reports it and one side rolls back: if the worker loses, the run is gone
  with the account; if erasure loses, the operator re-runs it. No partial state. Not tested by a harness.
- **A refund racing a charge** can make that charge read the balance just before the refund committed and
  answer `no_credits` once. Accepted; the next click works.
- **A charge costs a lock and a few hundred rows** on the request's path, held to < 30 ms p95 over `main`
  with a 200-entry history.
- **Shipped worker behaviour changes** (decision 6); its tests are re-read and any that encoded
  "SAVEPOINT only" are corrected in their own commit.
- **A deleted run's charge and refund keep its id**, a dangling reference, as ADR-0014's 2.3 amendment
  does for `base_cv_id`: an id, no content.
- **The downgrade refuses** while any entry other than an opening grant exists, because dropping the tables
  would then destroy something a user spent or was given.
