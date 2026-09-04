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
survives a purge run is written in the same slice as the purge, not later.

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
  assumes the purge covers it.
