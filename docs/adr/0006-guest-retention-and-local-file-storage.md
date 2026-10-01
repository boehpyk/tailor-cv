# ADR-0006: Guest retention of 24 hours, and files on a local volume behind a port

- **Status:** Accepted
- **Date:** 2026-09-04

## Context

TailorCraft accepts a CV **before** it knows who the user is (PRD §3: the guest is a first-class
persona, not a degraded one). That produces a category of data with no owner and no one to ask about
deletion: an uploaded CV, an extracted-text blob, a tailored document and a rendered PDF, attached to
nothing but a browser session.

A CV is dense PII — name, address, phone, employment history. PRD §9 sets the policy: guest data is
purged after one day. FR-6 makes it a functional requirement.

Separately, files have to live somewhere, and the `api` container that writes an upload is not the
`worker` container that renders from it.

## Decision

**1. A guest session is an explicit, expiring thing.** A `GuestSession` with a creation instant and an
`expires_at`, not a cookie that happens to hold an id. Every guest-owned row references it. The
purge predicate is **`expires_at` and nothing else** — not "was it downloaded", not "did they seem
finished". One column, one meaning, one index.

**2. The purge deletes rows and files, in that order, and can find orphans without a row.** Delete the
database rows in a committed transaction, then unlink the files. A crash between the two leaves an
orphaned *file*, which a directory-driven sweep can still find; the opposite order leaves a row
pointing at nothing, which presents to a user as a broken download. Choose the crash window whose
survivor is recoverable.

**3. Registered users' data is never touched by the guest purge.** The predicate selects guest
sessions; a base CV owned by a user has no guest session. The test that proves a registered CV
survives a purge run is written in the same slice as the purge, not later. *(Amended below: the
proof became testable in slice 2.2, when a base CV first had a user as its owner.)*

**4. Files live on a local named volume shared by `api` and `worker`, behind a `FileStorePort`.** The
port speaks in `FileRef`s, never in filesystem paths, and never hands out a path that reaches the
domain. Stored names are generated, never derived from the uploaded filename — a user-supplied name is
a path-traversal attempt waiting for somewhere to be joined.

**5. Retention is stated to the user**, in the UI, at upload time. A silent 24-hour delete is a
support ticket; a stated one is a feature ("we don't keep your CV — register to save it"), and it is
also the registration prompt the PRD asks for.

## Alternatives

- **Object storage (S3/R2) from day one.** Rejected for now (ADR-0003): one box, no bill, no second
  console. The port is exactly what makes this a later adapter rather than a migration — and it is the
  first thing to change if a second worker host ever appears.
- **Storing files as bytes in Postgres.** Rejected: it bloats the database, makes every backup and
  dump proportional to upload volume, and buys transactional consistency we can get more cheaply by
  choosing the crash window above.
- **A longer guest window (7 days) for convenience.** Rejected: the retention period is a privacy
  promise, and a longer one has to be justified by a user need that registration already serves better.
- **Deleting on session cookie expiry, client-side.** Not a policy. Data the server holds is deleted by
  the server, on a schedule, whether or not the browser ever comes back.

## Consequences

- **The purge is the first `DELETE` in the codebase and it is irreversible in two systems at once.**
  It gets a dry-run mode, a `--limit` for a small first bite, a backlog count on `/health/ready`, and
  one log line per run *including the runs that delete nothing* — see infrastructure.md's runbook. A
  job whose failure mode is silence needs a signal that a do-nothing job cannot fake.
- Backups must include the uploads volume, not only the database. A restored row pointing at a missing
  file is a broken app with a green restore.
- Phase 2's guest→registered **claim flow** is constrained by this design: registering must re-key the
  work to the user before the session expires, and that path needs its own test. Design it when
  Phase 2.4 is planned, not when a user complains. *(Designed in amendment (e) below and ADR-0025: not on
  registering, but on an explicit offer after it.)*
- GDPR erasure for registered users is a *different* mechanism and is not built here. Named so nobody
  assumes the purge covers it. *(Discharged by the amendment below: account erasure, slice 2.2.)*

## Amendment: 2026-09-25, from the plan of slice 2.2 (`intake-saved-base-cvs`)

This ADR was written for a product in which every stored byte belonged to a guest. Slice 2.2 adds
the first data the product keeps **on purpose** — a registered user's saved base CVs — and with it
the first user-initiated deletion and the first account erasure. The five decisions above stand; this
amendment adds three, and records how §3's obligation was met.

**(a) Two retention promises now coexist, and the UI states both.**

- **Workspace data: at most 24 hours, enforced by a schedule** — everything above, unchanged. A
  **working copy** (ADR-0022: a saved CV copied into the guest workspace so it can be tailored) is
  workspace data. It is owned by the browser's guest session, it dies with that session under the
  24-hour rule, and so does everything tailored from it. Deleting the saved CV does not delete its
  working copies, and neither does deleting the account; the guest purge does.
- **Saved data: kept until the user deletes it, enforced by a button.** A saved CV has a user as its
  owner and no guest session, so the purge's predicate and its cascade cannot select it. This is a
  **change to the product's privacy promise** — a CV kept indefinitely, by the user's choice — and §5's
  rule applies to it: it is stated where the user saves and where they reuse, not buried.

§3's obligation is now proven rather than asserted: a registered user's CV survives a purge run and an
orphan sweep **with rows and files that exist**, and a working copy's purge leaves its saved source
untouched — each test observed failing under a named mutation. The purge spares saved data **by
schema** (`guest_session_id IS NULL` on a saved CV); the orphan sweep spares it by its database
cross-check, which must therefore read every row's file key regardless of owner.

**(b) Deleting a saved CV is §2's order, applied to a request.** The row is deleted and the
transaction **committed**, then the file is unlinked. The survivor of a crash between the two is an
orphaned file, which `purge-guests --orphans` reclaims because its cross-check finds no row. An unlink
that fails after the commit is **not** an error to the user — the row, the only thing they can see or
reach, is gone, and a retry could only answer 404 — so the request succeeds, a warning names the id
and the exception type, and the bytes remain until an operator runs the orphan sweep.

**(c) Account erasure: rows committed, then files, under a row lock on the user.** One transaction
takes `SELECT … FOR UPDATE` on the user's row, collects the file keys of every CV the user owns,
deletes the user row — which cascades to logins, retired refresh hashes and saved CVs — and
**commits**; only then is each file unlinked. The lock closes the one race that "rows first" does not:
an upload inserting a CV *after* the keys were collected and *before* the delete would otherwise be
cascaded away with its file never collected. With the lock, that upload's foreign-key check waits and
then fails, the request answers "not signed in", and its already-written file is an orphan for the
sweep. Unlinks that fail are **returned** in the erasure's report, not logged from the application
layer (ADR-0018's pattern), and are reclaimed by the operator's orphan sweep. Erasure lives in the
`retention` context beside the purge — the same shape, *delete everything an owner has, rows then
files, reporting what could not be unlinked* — on request for a user rather than on a timer for a
guest. It is reached two ways: the user's own **Delete account**, which re-confirms the password, and
an operator command, `erase-account`, with a dry run. This discharges the Consequence above that named
GDPR erasure as a different mechanism not built here, and it makes "delete the user row by hand"
**wrong** as an operator procedure: the cascade would remove the rows and orphan every file.

## Amendment: 2026-09-26, from the plan of slice 2.3 (`tailoring-application-history`)

Slice 2.3 gives a registered user a history of their tailored documents (ADR-0023). Amendment (a)'s
two promises — workspace data for at most 24 hours, saved data until the user deletes it — stand. This
amendment adds a third kind of saved data and says how each deletion reaches it.

**(d) A registered user's tailoring is kept until they delete it, one entry or the whole account.**

- **What is kept.** A signed-in user's tailoring runs (the tailored CV and cover letter of record),
  the job postings they were made from, and their rendered PDF and DOCX export files. All are
  user-owned from creation, with no guest session, so the purge's predicate and cascade cannot
  select them — spared **by schema**, exactly as a saved CV is. The orphan sweep spares a kept export
  file by its database cross-check, which reads `export_job.file_key` for every row regardless of
  owner. Both are proven with rows and files that exist, each test observed failing under a named
  mutation. Like (a)'s saved CVs, this is a **change to the product's privacy promise**, and §5's rule
  applies: the account workspace and every account response state it (`expires_at: null` means *kept
  until you delete it*), not buried.
- **Deleting a history entry is §2's order, applied to a request.** One transaction deletes the run,
  its export jobs, and its posting **if no other entry uses it**, and commits; then each export file
  is unlinked. The keys are **derived** from the export rows the `DELETE` returned, so a `rendering`
  or `failed` job's already-written bytes are not missed, and nothing is read before it is deleted.
  An unlink that fails after the commit is not an error to the user, as in (b): the request succeeds,
  the failures are returned in the report, a warning names each, and the bytes remain until the
  operator's orphan sweep. A crash between the commit and the unlinks leaves orphans for the same
  sweep. A run that is still `queued` or `running` is refused, since nothing in this product cancels a
  paid call.
- **Deleting a saved CV does not reach history.** The entries made from it keep their documents and
  show *"CV deleted"*, derived at read time (ADR-0023 decision 4). They are separately owned
  documents the user may still be sending, and the saved-CV deletion dialog says so. Working copies
  are still reached by nothing but the guest purge, as (a) says.
- **Unused account postings** — captured, never tailored — are kept with the account, capped, and
  erased with it. There is no per-posting delete in 2.3. **Trigger:** any user above 100 unused
  postings.
- **Account erasure, (c), widens without changing shape.** The user row's cascade now also removes
  runs, postings and export jobs; the collected file keys are the saved CVs' **and** the derived keys
  of the user's PDF and DOCX export jobs; the report counts all of them. The `SELECT … FOR UPDATE` on
  the user row now also serializes run, posting and export `INSERT`s, since each foreign-key check
  takes `FOR KEY SHARE` on that row — so a racing request waits, then answers "not signed in", and
  any file it already wrote is an orphan for the sweep.

## Amendment: 2026-10-01, from the plan of slice 2.4 (`workspace-registration-cta`)

Slice 2.4 lets a signed-in user keep the work they did as a guest (ADR-0025). The promises of (a) and
(d) stand; this amendment adds a fourth way into *kept until deleted*, and says how the change is
stated.

**(e) Claimed guest work is kept until the user deletes it, and the offer says so before the click.**

- **What changes.** When a user accepts the claim, the guest session's base CVs, job postings,
  tailoring runs and export jobs become user-owned in one transaction, and from then on they are
  account data under (a) and (d): spared by the purge **by schema** (no guest session), spared by the
  orphan sweep's owner-blind cross-check, deletable one history entry or one saved CV at a time, and
  erased with the account. Their files do not move; keys derive from ids (ADR-0011 §1). Working
  copies are the exception: they are never claimed and are deleted with the session (ADR-0022 (d)), in
  §2's order — rows committed, then files, unlink failures returned and left to the orphan sweep.
- **It is a change to the privacy promise, stated where it happens** (§5's rule). Data the user was
  told would be *"deleted within 24 hours"* becomes *"saved until you delete it"*. The offer names the
  work — CV filenames and the number of tailored applications — and states both outcomes before either
  button: keep it and it is saved until deleted, otherwise it is deleted within 24 hours. A claim is
  never automatic, so nothing becomes kept without that sentence having been shown.
- **Declined work keeps the 24-hour promise.** *Not now* changes nothing on the server. Work the user
  declines, or never signs in for, is the purge's, as in §1.
- **The guest session ends with the claim.** Its row is deleted in the claim's transaction and its
  cookie cleared (ADR-0010's amendment), so nothing is left for the purge to find; the purge's own
  rule for a session a claim got to first is in ADR-0018's amendment.
