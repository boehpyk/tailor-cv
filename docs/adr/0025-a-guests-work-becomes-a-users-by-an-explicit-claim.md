# ADR-0025: A guest's work becomes a user's by an explicit claim: one transaction, every owned table, the session ended

- **Status:** Accepted
- **Date:** 2026-10-01
- **Relates to:** ADR-0006 (the 24-hour promise; §2's order — **amended (e)** alongside this ADR),
  ADR-0008 (f) (the transfer route — **amended (g)**), ADR-0010 (a guest session is not a weak
  login; its Consequence for 2.4 — **amended**), ADR-0011 §1 (keys derive from ids, so no file
  moves), ADR-0014 §4 (caps are soft rules in use cases that create), ADR-0018 (the purge —
  **amended**), ADR-0022 (a row's owner is a sum type; an ownership graph never crosses owners —
  **amended (d)**), ADR-0023 (the workspace follows the credential; its Consequence *"2.4's claim is
  four `UPDATE`s"* is executed here as written, so it is **not** amended). Supersedes nothing.

## Context

Slice 2.4 is the step Phase 2's gate turns on: a guest who tailored a CV, liked the result and then
registered must not lose it. Since 2.3 a signed-in user's work is born user-owned (ADR-0023), and every
owned table — `intake_base_cv`, `posting_job_posting`, `tailoring_run`, `export_job` — already admits
either owner (`ck_<table>_exactly_one_owner`). What is missing is the one operation that **changes a
row's owner after it was created**, which nothing in this codebase has done before.

Five forces meet:

1. **A guest cookie identifies a browser, not a person** (ADR-0010). On a shared computer the previous
   visitor's work sits in the browser for up to 24 hours. Whatever moves guest work into an account,
   where it is *kept until deleted*, must not move a stranger's CV.
2. **An ownership graph never crosses owners** (ADR-0022). A run, its CV, its posting and its exports
   must change owner together, or the purge deletes, a day later, a row a kept run still references.
3. **ADR-0010 obliges the claim to invalidate the guest token at the same moment** it re-keys the
   work; otherwise a stale cookie keeps read access to an account's data.
4. **The purge (ADR-0018) collects a session's file keys, then deletes the session, then unlinks.**
   Until now those keys could not stop being the session's in between. A claim makes that possible.
5. **Work may be in flight** — a run `queued` or `running`, an export `rendering` — and the worker
   writes its outcome under an optimistic `version` check (ADR-0015).

## Decision

**1. An explicit offer, never automatic.** After registering or signing in, a user whose browser holds
guest work sees it **named** — CV filenames, the number of tailored applications — and chooses **Keep
them in my account** or **Not now**. The offer is also where the retention change is stated, before
the click (ADR-0006 (e)). A claim is a separate request after authentication, never part of
`register` or `login`.

**2. One route: `POST /api/me/guest-work/claim`.** No body, no parameters. The bearer authorizes the
destination; the `tc_guest` cookie names the source and is read in the handler body; nothing is ever
minted (ADR-0008 (g)). It answers **200 with counts**, and **200 with zeros** when there is nothing to
claim — no cookie, an unknown, expired or already-claimed session. That is what makes it idempotent in
effect: the session row *is* the idempotency key, and the claim consumes it, so a retried request
whose first response was lost reads as success, not as an error. The server cannot distinguish
*"already claimed"* from *"never existed"* without keeping a record of the session it deleted, and it
does not keep one.

**3. One transaction moves everything the session owns.** After the session row is locked, four
`UPDATE`s — one per owned table, each `SET guest_session_id = NULL, user_id = :u WHERE
guest_session_id = :s` — then the working copies' `DELETE … RETURNING file_key`, then the session's
`DELETE`, then the commit. One statement sets both owner columns, so no row is ever ownerless or
doubly-owned, even inside the transaction. **No row is copied, no id changes and no file moves** (keys
derive from ids, ADR-0011 §1). A claimed run is in history with no further work, at its original
`requested_at`; the client's `/runs/{id}` becomes `/history/{id}`, same id. **Nothing is filtered by
status**: failed runs, failed extractions, unused postings and failed exports all move — the user can
delete what they do not want. Working copies (ADR-0022 §4) are never claimed; they go with the session
(ADR-0022 (d)). Files are unlinked after the commit (ADR-0006 §2), and unlink failures are returned in
the report, not logged by the application layer.

**4. The session is deleted in the claim's transaction, and the cookie cleared on its response.**
That is ADR-0010's *"at the same moment"*, by construction: a stale copy of the cookie names no row
and authenticates nothing. A later guest action in that browser starts a fresh, empty session. It is
also what gives the purge a **fact** to decide by (ADR-0018's amendment): *did my `DELETE` remove the
session?* is true exactly when no claim of it committed first.

**5. The lock order is session, then user, and every other actor takes at most one of them, or both
in the same order.** The claim takes the guest session row `FOR UPDATE` (by token hash), then the user
row `FOR KEY SHARE`, implicitly, through each `UPDATE`'s `user_id` foreign-key check. So:

| Actor | Outcome against a claim |
|---|---|
| Purge `DELETE` of the session | first wins; claim first ⇒ the purge deletes nothing and unlinks nothing; purge first ⇒ the claim finds no session, 200 zeros |
| Account erasure (user row `FOR UPDATE`) | erasure first ⇒ the claim's FK fails ⇒ 401, rolled back; claim first ⇒ erasure takes the claimed rows and files |
| A guest `INSERT` (its FK takes the session row `FOR KEY SHARE`) | waits; the session is gone ⇒ FK fails ⇒ 401 `guest_session_expired` |
| A second claim | waits; finds no session ⇒ 200 zeros |
| The worker / a guest edit (`UPDATE … WHERE version = :v`) | waits for the commit; `version` unchanged ⇒ succeeds |
| The orphan sweep | owner-blind cross-check ⇒ claimed files are referenced ⇒ spared |

No cycle exists. Each row is staged in a test at the moment the race names.

**6. In-flight work moves as it is.** The claim never waits for, cancels or refuses because of
`queued`/`running` runs or `queued`/`rendering` exports, because three facts make it safe: the worker
never authorizes (it loads by id); the worker never writes an owner column (the ORM updates dirty
attributes only); and **the claim never bumps `version`**, so the worker's `UPDATE … WHERE version =
:loaded` still matches. The third is load-bearing and mutation-proven: bumping `version` "for safety"
turns a paid result into a skipped one. The one in-flight outcome the claim *changes* is already
modelled: a queued run made from a working copy finds its CV gone and fails `base_cv_deleted` before
the paid call (ADR-0014's amendment). A guest edit that loaded a run before the claim and saves after
lands on the now user-owned run — the same browser's edit of the same document; accepted and stated.

**7. Caps bound creation, not transfer.** Every cap in this product is a soft rule in a use case that
**creates** (ADR-0014 §4); a claim creates nothing, so it never fails on a cap. An account may sit
above one after a claim, and the next creation of that kind answers the existing 409 until the user
is back under it. The excess per claim is bounded by one guest session's caps, and claims are
rate-limited to 10 per hour per user (failing open — the cost is ours and bounded). The client renders
an over-cap account honestly. **Trigger:** any account above twice a cap.

**8. The claim lives in `identity`, with no aggregate and no domain event.** `identity` because the
claim is the hand-off between the two principals ADR-0010 kept apart, and the `Owner` type it rewrites
lives in `domain/identity/ownership.py`. No aggregate because its invariant — *every row of a session
changes owner together* — spans four contexts' tables and is enforced by **one transaction in one
adapter**, exactly as ADR-0018 argued for the purge; the one domain rule it applies,
`GuestSession.is_expired`, stays on `GuestSession`. No event because its only listener would be the
logger the router already is, and a `GuestWorkClaimed(guest_session_id, user_id)` payload would be the
one durable record pairing a browser session with an account. The domain has a port
(`GuestWorkClaimPort`: `lock_session`, `transfer` — the method order is the lock order) and two value
objects (`ClaimedGuestWork`, `GuestWorkClaimReport`). The port names no other context's aggregate —
counts and `FileRef`s only; the adapter may import the four `Table`s, as account erasure's does. The
router logs counts and never the session id beside the user id.

## Alternatives

- **Claim automatically on every login and registration.** Rejected: the shared-computer case. A
  stranger's CV would land in this user's account, kept until deleted.
- **Claim automatically when registration started from the CTA**, carrying the intent through `next=`.
  A fair argument — the CTA's click is consent about work the user could see — but the intent rides in
  an attacker-chosen URL, needs a second path into the claim, and saves one click. The reasonable
  fallback if zero friction is ever wanted on that path.
- **Claim inside `POST /api/auth/register` / `login`.** Rejected: it puts a four-table cross-context
  write on the `Origin`-checked, limiter-first cookie surface (ADR-0021), ties registration's latency
  and failure modes to it, and is the automatic claim with a worse blast radius.
- **A per-run claim ("save this run").** Rejected: it would move a run without its CV or posting, or
  drag them along one at a time — an ownership graph cut by a request.
- **Copy the guest rows into the account, then delete the originals.** Rejected: new ids break the
  client's links, duplicate a run's cost facts (ADR-0014 §2), and copy files for no gain. Re-keying
  changes one column per row.
- **Refuse the whole claim when a cap would be exceeded.** Rejected: an existing user with five saved
  CVs who tailors once as a guest could not keep that run without first deleting a CV they want — it
  fails *"loses nothing"* for a real case.
- **Claim up to the cap and drop the rest.** Rejected: which rows? A graph cut by a cap crosses owners.
- **Refuse (409) while anything is in flight.** Simpler to state, and it blocks the claim for as long
  as the worker is down, since `queued` runs stay queued through an outage.
- **Expire the session (`expires_at = now`) instead of deleting it.** Rejected: the purge's later
  `DELETE` would then remove the row, so the purge race (ADR-0018's amendment) would have no fact to
  decide by, and a still-resolving token is one more path to reason about.
- **Leave the session alive.** Rejected: ADR-0010's obligation, and a later upload would land in a
  live guest session outside the account the user just chose.
- **The claim in `retention`**, which already spans the four tables with Core statements. Rejected:
  retention *deletes* what an owner has; the claim *keeps* it. The working-copy deletion is a tail of
  the claim, not its meaning.
- **An aggregate, or a `GuestWorkClaimed` event.** Rejected: decision 8.
- **A preview route** returning what a claim would move. Rejected: it would be a second route reading
  both credentials — a second transfer route — to compute numbers the client already has from the
  guest lists, fetched with the cookie alone. The claim's response carries the authoritative counts.
- **Record that a row was claimed** (a column or flag). Not now: a claimed row is an ordinary user row,
  and nothing reads the difference. Phase 3 can add one if it ever needs to know.

## Consequences

- **The first owner change after creation is one transaction.** The only way a graph can be half
  re-keyed is a commit in the wrong place, and the adapter commits exactly once, after the session's
  `DELETE`.
- **A future owned table must join the claim**, or a claim leaves it guest-owned and the session's
  `DELETE` cascades it away. A test asserts that every table with a `guest_session_id` column is
  re-keyed by `transfer`, so a fifth owned table turns it red rather than losing data.
- **The purge changes for the first time since 1.6** (ADR-0018's amendment): it unlinks a session's
  keys only when its own `DELETE` removed the session. Without that, a claim committing between the
  purge's read and its delete would have the purge unlink files a user now owns.
- **Claimed work joins *kept until deleted*** (ADR-0006 (e)) — a change to the privacy promise, stated
  at the offer. Declined work keeps the 24-hour promise.
- **The `version` column's boundary is now explicit**: it guards what it is bumped for, and the claim
  deliberately does not bump it. A future "bump it for safety" discards worker results; a test with a
  named mutation and the adapter's docstring both say so.
- **No migration.** Every CHECK, cascade and `guest_session_id` index the claim needs exists already;
  the slice asserts that by reading the catalogue, not by trusting comments.
- **Accounts can sit above a cap**, bounded per claim and by the limiter, with the trigger above.
