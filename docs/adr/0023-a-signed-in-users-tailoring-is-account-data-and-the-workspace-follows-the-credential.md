# ADR-0023: A signed-in user's tailoring is account data, and the workspace follows the credential

- **Status:** Accepted
- **Date:** 2026-09-26
- **Relates to:** ADR-0006 and its amendments (two retention promises; §2's order — **amended (d)**
  alongside this ADR), ADR-0008 amendment (f) (one credential per route, except a named transfer
  route — **not** amended: this ADR adds no transfer route), ADR-0014 (the run, its failure reasons,
  and the *"nullable reference plus state"* alternative — **amended** alongside this ADR),
  ADR-0015 §1 and ADR-0014 §9 (the `TailoredDocument` promotion trigger — examined below, not
  reached), ADR-0016 (export jobs and their cap — **amended** alongside this ADR), ADR-0018 (the
  purge; decision 3's derived export keys), ADR-0022 (a row's owner is a sum type; an ownership graph
  never crosses owners — **amended** alongside this ADR), ADR-0024 (the history read model).
  Supersedes nothing.

## Context

Slice 2.3 gives a registered user a **history**: the tailored CVs and cover letters they made, kept
until they delete them, re-openable, re-editable and re-exportable. Until now every tailoring run,
job posting and export job belonged to a guest session and died within 24 hours (ADR-0006). ADR-0006
and ADR-0018 already let exactly one kind of row outlive that window: a row with **no guest session**.
So a history run is a user-owned run, and 2.2 built the type for it (ADR-0022's `Owner`). The open
question is not *whether* runs become ownable by a user, but **how a run comes to have that owner**,
and what that does to the three rows around it.

Five forces meet:

1. **ADR-0022's rule is that an ownership graph never crosses owners.** A run's base CV and posting,
   and an export's run, must have the run's owner. Otherwise the 24-hour purge deletes, a day later,
   a row a kept run still references — or a user's deletion reaches into a stranger-lifetime row.
2. **ADR-0008 (f): one credential per route**, except a named transfer route, and nothing ever asks
   *"a user or a guest?"*. The existing guest routes cannot start accepting a bearer.
3. **A run's cost is one immutable fact** (ADR-0014 §2). Whatever makes a run the user's must not
   duplicate it.
4. **Slice 2.4 (the claim) is next.** It re-keys a guest's work to the user. Whatever 2.3 decides
   must not pre-empt it, or foreclose it.
5. **A saved CV is deletable** (2.2), and from now on user-owned runs may be made from one. 2.2's
   `tailoring_run.base_cv_id` comment named this slice as the moment that question needs an answer.

**Examined and not triggered.** ADR-0014 §9 and ADR-0015 §1 name the trigger for promoting
`TailoredDocument` to an aggregate: *a revision history, a document shared across runs, or an export
that needs a document id*. History lists **runs**; each run still holds one current revision per
document; exports stay keyed by (run, document kind). The trigger is not reached, and this paragraph
is here so the next reader need not re-derive that.

## Decision

**1. The workspace follows the credential.** Signed in, the posting, the run and its exports are
**created user-owned**, and the run references a **saved CV directly**. Signed out, the workspace is
the guest workspace of slices 1.1–1.6, unchanged. Every row a history entry touches is therefore born
with its final owner: nothing is ever moved or re-keyed, ADR-0022's rule holds by construction, no
route needs both credentials, and the purge cannot reach anything the user kept. This is ADR-0022's
predicted consequence — *"a user-owned run may then reference a saved CV directly — no copy needed"* —
taken literally.

Which workspace a page is in is decided **by the route**, never at request time. A client that asked
*"is someone signed in right now?"* before each call would send a guest run's autosave to the
account's routes the moment a user signed in in another tab. The URL owns the scope as it owns the
run id.

**2. Account twins under `/api/me/`, with the bearer alone, and one handler body per pair.** The
account gets its own routes for postings, runs, documents, downloads, exports and export jobs under
`/api/me/` (2.2's precedent: `/api/me/base-cvs` beside `/api/base-cvs`). To stop twelve twins
becoming twelve drifting copies, each handler's **body** is a credential-agnostic function taking an
already-resolved requester; the guest router and the account router are only their decorators, their
credential dependency and a one-line call. That extraction lands before any twin exists, with every
existing API test passing unchanged — the proof it is a refactor.

**No transfer route is added.** An account run's inputs are all account data, so no request carries
data between principals. ADR-0008 (f)'s exception set stays exactly `{POST /api/base-cvs/copies}`,
and 2.2's dependency walker plus AST scan re-assert it over the new modules.

**3. Use cases are requester-generic: one resolution, one equality.** One `resolve_owner` turns a
requester into a resolved `Owner` (an active guest session, or an existing user) with a `match` and
`assert_never`; every read is then `row.owner != owner → not found`, the 404 collapse unchanged. The
worker is **not** generalized, because it never authorizes: it loads by id, as it did in 1.3.

**4. Deleting a saved CV is allowed, and history keeps what was made from it.** The run's `base_cv_id`
**dangles** — it stays as history (*"made from that"*), exactly like 2.2's `copied_from_base_cv_id` —
and *"CV deleted"* is **derived at read time** by the history read model's `LEFT JOIN` (ADR-0024). No
foreign key, no stored flag, no write to any run when a CV is deleted. The one reachable failure this
creates — a run requested, its CV deleted in another tab, the worker reaching it afterwards — is
recorded as `failed` / `base_cv_deleted` **before the paid call** (ADR-0014 amendment).

**5. Deleting a history entry is `retention`'s.** *Delete everything an owner has for X, rows
committed, then files, reporting what could not be unlinked* is the shape `retention` already owns
twice (the purge, account erasure). An entry spans three tables — the run, its export jobs, and its
posting **if no other entry uses it** — and a set of files, so `EraseHistoryEntry` lives in
`application/retention/` behind a `HistoryEntryDataPort`, not as a `remove` on
`TailoringRunRepository`, which would have to delete other contexts' rows. The adapter deletes in one
transaction with `DELETE … RETURNING`: the run first (zero rows — a concurrent delete won — means the
loser touches and unlinks nothing), then the export jobs, whose returned `(id, format)` pairs give the
file keys by derivation (`FileRef.for_export`, ADR-0018 decision 3's rule, so a `rendering` or
`failed` job's already-written bytes are not missed). *"The rows I deleted"* and *"the files I must
unlink"* are one set, with no window between a read and a delete. A `queued` or `running` run is
refused (409): there is no cancellation in this product, and a delete racing the worker's outcome
write would either lose documents the user just paid for or leave a row the delete claimed was gone. The
posting's `NOT EXISTS` check is not enough alone: it cannot see a concurrent request's uncommitted
run on the same posting. The deletion therefore locks the posting (`FOR UPDATE`) and deletes it in a
separate statement. The run insert takes `FOR KEY SHARE` on the posting after its INSERT and refuses
a posting that is gone. Neither side can be switched off (amended at `/verify`, 2026-09-30).

**6. The caps are per user, soft, in the use case** (ADR-0014 §4's reasoning — they span
aggregates): 500 runs and 500 postings per user, 20 export jobs **per run**. At the run cap the 409
tells the user to delete older entries. Guest caps are unchanged.

**7. The 2.2 working-copy route stays, with no first-party caller.** The account workspace references
a saved CV directly, so the UI stops calling `POST /api/base-cvs/copies`. The route, its use case,
`copied_from_base_cv_id`, the *Working copy* badge and ADR-0008 (f)'s exception **stay unchanged**:
the deploy runs two versions briefly, existing working copies live out their 24 hours, and **slice 2.4
decides** what a working copy is once a guest's work can become the user's. Removing the route now
would make that decision early and half.

**8. Application tracking is a new aggregate, not columns on the run.** Phase 3 will want the status
of an *application* — applied, interviewing, rejected — with notes and dates, beside the run. A run's
status is a fact about a paid call; an application's is a fact about the user's job search. When it
comes, it is a new aggregate referencing a run or a posting **by id**, and it reads through
ADR-0024's query-port pattern. `tailoring_run` does not grow those columns.

## Alternatives

- **"Save to history" on a finished guest run** — a transfer route copying or re-keying the run, its
  posting and its CV from guest to user. Rejected: it is a per-run claim, and it pre-empts 2.4 across
  three tables. A *copy* duplicates the run's cost facts (ADR-0014 §2); a *re-key* drags a guest CV —
  maybe a working copy — and a posting into the account one at a time. It would also be a third
  transfer route, which ADR-0008 (f) says is an ADR, not a list edit.
- **A user-owned run over guest inputs** (a guest posting, a working copy). Rejected: it crosses
  owners, and the purge would delete, 24 hours later, rows a kept run references. Forbidden by
  ADR-0022.
- **Both workspaces on one page**, with a per-run *"save to history"* toggle when signed in.
  Rejected: two ownership graphs rendered side by side, two sets of queries, and a default someone
  must choose. The guest workspace is one sign-out away; a toggle buys a mode nobody asked for.
- **An `ON DELETE SET NULL` foreign key and a nullable `base_cv_id`.** Rejected: a cross-context FK
  ADR-0014 declined; the run's invariant *"a run always has a base CV id"* weakens; a CV deletion
  writes to up to 500 runs; and the id — harmless — is destroyed for no gain.
- **Refuse a saved CV's deletion (409) while an entry references it.** Rejected: it makes a user's
  right to delete their CV hostage to their history.
- **Cascade: deleting the CV deletes its history.** Rejected: it silently erases paid-for documents
  the user may still be sending.
- **Accept a bearer on the existing guest routes.** Rejected: ADR-0008 (f). A route that asks *"a
  user or a guest?"* is the conflation that amendment exists to forbid.
- **Copy the React components into an account folder.** Rejected in favour of injecting the
  difference: a scope provided by the route, mapped to paths, the auth option, query-key roots and
  link prefixes, consumed by the hooks. The same drift 2.2 warned about on the server, in TypeScript.
- **Remove the copy route, its use case, the badge and the walker exception now** (an
  expand→contract removal, one more release). Rejected for now: decision 7.

## Consequences

- **A signed-in user can no longer tailor "without saving" in that browser.** Signing out is how.
  Guest work done *before* signing in is not in history until 2.4's claim; the account workspace
  **names** that work and links to it rather than hiding it — 2.2's objection to this option
  (*"hides the guest work the moment they sign in"*) is answered by naming, not by a toggle.
- **The product keeps tailored documents indefinitely, by the user's choice.** That is the largest
  change to its privacy promise since 2.2, and it is stated where it happens (ADR-0006 (d)). The
  client keys all account server state under the user and clears it with the account, so documents
  do not outlive a sign-out in memory either.
- **2.4's claim is four `UPDATE`s.** For each owned table, `SET guest_session_id = NULL, user_id = :u
  WHERE guest_session_id = :s` — every CHECK already permits either owner, and nothing here adds a
  *"created as guest ⇒ stays guest"* rule. Claimed runs appear in history with no further work,
  because history is *"user-owned runs"*. The claim must re-key a run, its posting, its CV and its
  exports **together**; 2.3's no-crossing test is the one 2.4 can reuse to prove it did. The caps will
  need a decision for a claim that would push a user over one. No file moves in either slice — keys
  derive from ids (ADR-0011 §1).
- **The copy route's caller is gone**, and 2.4 carries the trigger: *remove the copy route or give it
  a caller*.
- **A kept export costs disk.** A user at 500 runs × 4 exports is at most ~400 MB in the worst case,
  ~16 MB typically. Superseded exports are not pruned; the trigger is in ADR-0016's amendment.
- **Twelve new routes share their bodies with their guest twins**, so a fix to one is a fix to both
  by construction — and a behaviour that must differ has to be passed in explicitly (the requester,
  the expiry, the limiter scope, the URL prefix), where a reviewer can see it.
- **The residual, accepted and stated:** an export *request* that loaded a run just before its entry's
  deletion committed can insert its job just after. There is no FK from `export_job` to
  `tailoring_run` (ADR-0016 kept contexts' tables unfused), so the row lands; the worker finds no run
  and writes no file; the row is listed nowhere and is erased with the account. An FK would close it
  at the price of a cross-context constraint and a validating migration on a populated table, for one
  row with no file. **Trigger:** a user-owned export row with no run found in production.
- **Unused account postings** (captured, never tailored) accumulate with no per-item delete: capped at
  500, erased with the account. **Trigger:** any user above 100 unused postings.
