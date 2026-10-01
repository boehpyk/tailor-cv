# ADR-0022: A row's owner is a guest session or a user, and reuse across owners is a copy

- **Status:** Accepted
- **Date:** 2026-09-25
- **Relates to:** ADR-0006 §3 (registered data is never touched by the guest purge — this ADR makes
  that expressible), ADR-0007 (value objects map through `TypeDecorator`s, not composites), ADR-0008
  and its amendment (f) (the transfer route this ADR's copy needs), ADR-0010 (a guest session is not a
  weak login), ADR-0011 §1 (a file key is derived from the object's id), ADR-0018 (the purge and the
  orphan sweep this ADR must not break). Supersedes nothing.

## Context

Until slice 2.2 every `intake_base_cv` row belonged to a guest session: `guest_session_id` was
`NOT NULL`, the purge's cascade ran through it, and ownership was one column meaning one thing. Slice
2.2 lets a registered user **keep** base CVs — the first row in this codebase that outlives the
24-hour promise on purpose. Four forces meet:

1. **The domain question has exactly two answers.** *Who does this row belong to?* — a guest session
   or a user, never both and never neither. A model that allows either of the other two shapes will
   eventually hold one, and a row with no owner is a row nobody can authorize.
2. **A foreign key points at one table.** Postgres cannot store "a guest session *or* a user" in one
   column and keep referential integrity. The purge (ADR-0018) and account erasure both depend on a
   cascade, so dropping the foreign key is not free.
3. **The workspace stays a guest workspace.** 2.1 kept it guest-owned whether or not anyone is signed
   in, and 2.2 keeps it: tailoring runs, postings and exports remain guest data until slice 2.3. So a
   CV the user saved must somehow reach a run whose owner is a browser session with a 24-hour life.
4. **Two lifetimes meeting on one object is how deletion goes wrong.** The purge deletes a session's
   rows and **unlinks the files those rows name**. Anything a guest session can reach, the purge can
   destroy; anything a registered user can delete, a guest run might still be pointing at.

## Decision

**1. The owner is a sum type in the domain.** `Owner = GuestOwner(GuestSessionId) | UserOwner(UserId)`,
two frozen dataclasses in `domain/identity/ownership.py` — `identity` because every context already
imports `GuestSessionId` from there, and `domain/shared` must not import a context. A row with two
owners or none is **unconstructable**. `Owner` carries **no behaviour** — not `is_guest()`, not
`matches(requester)` — because the one operation anyone needs is `==`: authorization is value
equality (`cv.owner == UserOwner(requester)`), not a role check, and a predicate method would invite
exactly the `if owner.is_guest() or …` conflation ADR-0010 forbids. Code that must branch uses `match`
with `assert_never`, and `mypy --strict` is the exhaustiveness checker. *Owner* is a word about rows,
never about requesters.

**2. The database stores a product type and restores the rule with a `CHECK`.** Two nullable foreign
keys — `guest_session_id` and `user_id`, each `ON DELETE CASCADE` — and
`ck_intake_base_cv_exactly_one_owner: num_nonnulls(guest_session_id, user_id) = 1`. **The CHECK is not
the model; it is the second lock.** The model is the domain type; the CHECK makes a hand-written
`INSERT`, a botched migration or a future adapter bug fail loudly instead of producing a row nobody
can authorize. Both layers hold the rule, so it is testable with no database *and* true of every row.

**3. The mapping is the one place that translates, and it says so.** `BaseCv` keeps two private
mapped attributes written only by `_assign_owner(owner)` (a `match` with `assert_never`) and exposes
one public `owner` property that rebuilds the variant. The class comments the contradiction at the
point of contradiction: *two attributes, one fact, because a foreign key has one target.*

**4. Reuse across owners is a copy — an ownership graph never crosses owners.** A saved CV reaches
the workspace as a **working copy**: a new `BaseCv` built by the named constructor
`BaseCv.copy_from(source, id, into: GuestOwner, file, at)`, with its own id, its own file (the bytes
copied), the source's extracted text (no re-extraction), and `copied_from = source.id`. The source is
never mutated. Every row a tailoring run, an export or the guest purge can reach therefore has the
**same owner as the run**. The copy is created by a transfer route (ADR-0008 amendment (f)).

**5. The bytes are copied, not shared.** `file_key` is `UNIQUE` and ADR-0011 §1 derives a key from the
*object's* id. Sharing the source's key would need a second row with the same key (refused) or a
reference from the copy to the source's file — and then the purge, deleting the copy's session, would
unlink `FileRef.for_base_cv(source_id)`: **the saved CV's bytes**. A copy costs at most 10 MB of disk
for at most 24 hours and makes that structurally impossible. The file is copied at all — although
nothing downstream reads a base CV's bytes after extraction — because a `BaseCv` has exactly one
`FileRef` (invariant I-1), and "a base CV except it has no file" would be a second aggregate wearing
the first one's name.

**6. Provenance is a column with no foreign key.** `copied_from_base_cv_id` is a nullable UUID with
**no FK, on purpose**. A copy is guest data and its source is user data; an FK would be the one
cross-owner edge in the graph. With `SET NULL`, deleting a saved CV would write to a
stranger-lifetime row; with `RESTRICT`, a working copy would block its owner's deletion. It is
history — "this was copied from that" — and history does not hold a lock on its subject. It exists
for the wire's `origin` field and for slice 2.4's claim.

## Alternatives

- **A SQLAlchemy `composite()` over the owner.** Rejected: ADR-0007 maps value objects through
  `TypeDecorator`s, and a composite over a sum type needs `__composite_values__` on each variant —
  SQLAlchemy's protocol inside `domain/`. A one-field dataclass variant would also be read as a
  one-column composite: wrong arity, discovered at mapper configuration.
- **One `owner_kind` column plus one `owner_id` column.** Rejected: no foreign key at all. The purge's
  cascade would stop reaching guest rows, erasure would have nothing to cascade through, and
  integrity would move into application code — the one place it cannot hold under concurrency.
- **A separate `intake_saved_base_cv` table.** Rejected: two tables for one aggregate. Every query
  that must see *all* CVs — the orphan sweep's cross-check above all — becomes a `UNION` someone
  forgets, and that forgetting deletes a registered user's file.
- **A base class `OwnedAggregate`.** Rejected: shared shape is not shared behaviour (CLAUDE.md), and
  the next owned aggregate's rules will differ.
- **A guest run references the user-owned CV directly.** Rejected: a cross-owner reference. Deleting
  the saved CV would have to cascade into, or be blocked by, a stranger-lifetime run;
  `tailoring_run.base_cv_id` has no FK on purpose, so the run would silently point at nothing. It
  would also need both credentials on `POST /api/tailoring-runs`, the hot and paid route.
- **The client downloads the bytes and re-uploads them as a guest.** Rejected: it keeps one
  credential per request, but re-extracts (up to 3 s), spends the guest upload limit, puts CV bytes on
  a download route, and makes the browser orchestrate a domain operation.
- **Make the workspace user-owned when signed in.** Rejected: it pre-empts slice 2.3 across four
  contexts and, without 2.4's claim, hides the guest's work the moment they sign in.
- **Share the file between the copy and the source.** Rejected: decision 5.

## Consequences

- **The orphan sweep's database cross-check must stay owner-blind.** It reads
  `intake_base_cv.file_key` for every row regardless of owner, and that is what spares a saved file
  older than any window. Making it owner-aware — filtering on `guest_session_id IS NOT NULL`, say —
  would delete registered users' files. A mutation-proven test guards it, and the adapter's docstring
  says why. The purge itself spares saved CVs **by schema**: `guest_session_id IS NULL` on a saved
  CV, so the cascade from `identity_guest_session` cannot reach it.
- **Signing in does not make work permanent.** A working copy, and everything tailored from it, dies
  with its workspace within 24 hours; the saved CV is untouched. The UI states this at copy time.
- **Slice 2.3 inherits the shape per table.** Making runs, postings and exports ownable by a user is
  the same migration per table (`user_id` plus the CHECK) and the same `Owner` type. A user-owned run
  may then reference a *saved* CV directly — no copy is needed when both are the user's — and 2.3 must
  decide what happens when a user deletes a CV that a surviving run references.
- **Slice 2.4 inherits the copy decision.** The claim re-keys guest rows to the user, which now
  includes working copies: claimed, they would become saved CVs duplicating their source.
  `copied_from_base_cv_id` lets 2.4 drop or keep them, and 2.2 deliberately adds **no** "copied ⇒
  guest-owned" CHECK that would make either choice illegal. The claim is the second transfer route
  under ADR-0008 (f).
  *(Decided in amendment (d) below: working copies are never claimed.)*
- **No file ever moves.** Keys derive from object ids (ADR-0011 §1); a claim changes an owner column,
  never a key.
- **The copy is not idempotent.** Two clicks make two working copies. The client guards the click; a
  server-side idempotency key would be machinery for a double click on a free, 24-hour, guest-capped
  operation.

## Amendment: 2026-09-26, from the plan of slice 2.3 (`tailoring-application-history`)

Slice 2.3 makes tailoring runs, job postings and export jobs ownable by a user, so that a registered
user can keep a history (ADR-0023). This ADR's Consequences named that as the per-table extension of
the shape decided here, and named two questions for 2.3 to answer. The six decisions above stand;
this amendment records how the extension was executed, restates what §4 actually requires, and
settles the status of the copy route. The question of deleting a saved CV that a surviving run
references is answered in ADR-0023 decision 4 and ADR-0014's amendment: allowed, with a dangling
reference and a derived *"CV deleted"*.

**(a) The shape is extended per table, unchanged.** `posting_job_posting`, `tailoring_run` and
`export_job` each gain what `intake_base_cv` got in 2.2: a nullable `user_id` (`ON DELETE CASCADE`
from `identity_user`), a now-nullable `guest_session_id`, an index leading on `user_id`, and
`ck_<table>_exactly_one_owner: num_nonnulls(guest_session_id, user_id) = 1`. In the migration the
CHECK is validated **before** `guest_session_id` loses `NOT NULL`, so there is no instant at which an
ownerless row is permitted. `JobPosting`, `TailoringRun` and `ExportJob` each carry the same two
private mapped attributes, the same `_assign_owner` with `assert_never`, and the same `owner`
property — written three more times, on purpose, with **no shared base class** (§ Alternatives:
shared shape is not shared behaviour), and each class comments the sum-versus-product contradiction
at its own point of contradiction.

The orphan sweep's owner-blind cross-check now matters for a second column: it reads
`export_job.file_key` for every row regardless of owner, and that is what spares a user's ready
export file older than any window. The Consequence above about keeping the cross-check owner-blind
applies to both tables, and is proven for both by a mutation-observed test.

**(b) The rule is *"an ownership graph never crosses owners"*; the copy was the means.** §4's title
reads as though reuse always copies. What it requires is that every row a run, an export or the purge
can reach has the **same owner as the run**. In 2.2 a saved CV could only reach a *guest* run, so a
copy was the only way to satisfy the rule. From 2.3 a *user-owned* run references a saved CV
directly, because both are the user's and nothing crosses. The rule holds because every input to a
run is authorized against the **same resolved requester**, every created row takes that requester as
its owner, and an export job takes **its run's** owner. That last invariant spans aggregates, so it
lives in `RequestExport` with a comment saying so, and a database-level test checks it across every
2.3 flow. The alternative this ADR rejected — *"make the workspace user-owned when signed in"* — was
rejected for 2.2 because it pre-empted 2.3. ADR-0023 is that slice, and it takes the option up with
the rejection's second objection answered: guest work is **named** and linked from the account
workspace, not hidden.

**(c) The copy route stays, with no first-party caller from 2.3.** The account workspace no longer
calls `POST /api/base-cvs/copies`. The route, `CopySavedBaseCvToWorkspace`, `copied_from_base_cv_id`,
the *Working copy* badge and ADR-0008 (f)'s exception are **unchanged**: an open 2.2 tab may still
call it while the deploy runs two versions, existing working copies live out their 24 hours, and
slice 2.4 decides what a working copy *is* once a guest's work can become the user's — which the
Consequence above already hands it. Removing the route in 2.3 would make that decision early and
half. **Trigger, carried by 2.4:** remove the copy route, or give it a caller. *(Discharged by
amendment (d) below: removed.)*

## Amendment: 2026-10-01, from the plan of slice 2.4 (`workspace-registration-cta`)

Slice 2.4 builds the claim (ADR-0025), and the Consequence above and amendment (c) hand it two
questions: what a working copy *is* once a guest's work can become a user's, and whether the copy route
is removed or given a caller. Decisions 1–3 and 5 stand, and so do (a) and (b): *an ownership graph
never crosses owners* is exactly what the claim preserves by moving a session's rows together.

**(d) The copy route is removed; a working copy is never claimed; the readers stay until no working
copy can exist.**

- **The route is removed, in expand → contract terms, stage 1, by behaviour.** Since 2.3 the account
  workspace references saved CVs directly, so the copy has no first-party caller and no screen on which
  one would make sense: a signed-in user's `/` is the account workspace, and a signed-out user has no
  bearer. A transfer route with no caller is attack surface with no user. What **creates** working
  copies goes now: `POST /api/base-cvs/copies` and its request schema, `CopySavedBaseCvToWorkspace` and
  its wiring, `BaseCv.copy_from` (decision 4's named constructor), the `BaseCvCopied` event, the two
  errors only the copy raised and their mappings, ADR-0008 (f)'s exception for it, and the client's
  copy mode. The copy's tests go with the code they test, in the removal commit, named in its body.
- **What reads working copies stays**, because rows created before the release live out their 24
  hours: `copied_from_base_cv_id` (decision 6's column, its mapping and `BaseCv.copied_from`),
  `BaseCv.origin` and `BaseCvOrigin`, the wire's `origin` field, the *Working copy* badge, and the
  claim's `copied_from_base_cv_id IS NULL` clause. The deploy window is safe in both directions: a 2.3
  client never calls the route, and the column the old process maps is still there.
- **A working copy is never claimed.** It is by definition a copy of a CV some account already keeps.
  If the claimant owns the source, nothing is lost — it is in their saved list. If **another account**
  owns it (a user copied it into this browser, signed out, and someone else signs in), claiming it
  would move that account's CV into this one, kept until deleted. If the source was deleted, its owner
  chose that, and a claim must not undo the choice. One rule covers all three: the claim leaves working
  copies guest-owned and deletes them with the session — `DELETE … RETURNING file_key` in the claim's
  transaction, the files unlinked after the commit. Runs made from a working copy **are** claimed and
  dangle, and history derives *"CV deleted"* (ADR-0023 decision 4) with no new code. Re-pointing such a
  run to its source when the claimant owns it was considered and rejected: machinery for a row that,
  after 2.3's release, only a non-first-party client could have created.
- **Contraction trigger.** The first slice that migrates `intake_base_cv` after 2.4's release plus 24
  hours — or any time after that, on a production read showing **zero** rows with
  `copied_from_base_cv_id IS NOT NULL` — drops the column, `copied_from`, `origin`, `BaseCvOrigin`, the
  badge and the claim's clause, in one contract migration. Its downgrade need refuse nothing, since the
  column would be all-`NULL`. The trigger is also carried in the roadmap and in the domain's docstring.

Decision 6's *"it exists for the wire's `origin` field and for slice 2.4's claim"* is thereby used
once, as an exclusion, and then retired with its readers.
