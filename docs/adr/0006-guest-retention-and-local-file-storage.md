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
  Phase 2.4 is planned, not when a user complains.
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
