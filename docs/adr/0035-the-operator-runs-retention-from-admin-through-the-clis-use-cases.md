# ADR-0035: The operator runs retention from /admin through the CLI's use cases: one guest session is erased by the purge's own step, a held purge lock is a 409, and storage is attributed by one cross-check over the existing walk

- **Status:** Accepted
- **Date:** 2026-10-13
- **Relates to:** ADR-0008 (auth — **amended** the same day: amendment (l), a path-named
  `GuestOwner` behind `require_admin` only), ADR-0018 (the purge — **amended** the same day:
  amendment (c), a third entry point and a second caller of its step), ADR-0006 (guest retention —
  note added), ADR-0019 (`/health/ready`'s facts — note added), ADR-0024 (query ports — note added),
  ADR-0034 (admin content through the owner's own use cases — note added). Supersedes nothing.

## Context

Slice 4.4 (`admin-guests-and-storage`) closes Phase 4. In `/admin` the operator lists live guest
sessions, reads one session's data, deletes a session before the 24-hour purge would, sees how the
uploads volume splits between guests, users and nothing, runs the guest purge and the orphan sweep,
and reads job health. Until now every one of those needed SSH: `psql`, `du`, and the CLI's
`purge-guests` and `--orphans`.

The owner's Phase 4 decisions bound the design: **Read + Delete only, through the existing erasure,
purge and sweep use cases** — a `DELETE` written for /admin alone would be a second implementation
that drifts — and **deleting one guest session is the existing purge, narrowed to that session**
(OQ-E). Admin actions log ids only; there is no audit table.

Three forces pull against simple reuse. The purge selects by `expires_at <= now`, and a session the
operator wants gone now is by definition not expired. The purge is guarded by an advisory Redis lock
the CLI reports as exit 3 and the task as a skip; an HTTP route needs its own honest answer for "did
nothing because someone else holds it". And "how much is on the volume, and whose?" has no use case
at all — the orphan sweep walks the volume, but only to find files no row names.

## Decision

1. **Erasing one guest session is the purge's per-session step.** The body of
   `PurgeExpiredGuestSessions`'s per-candidate loop is lifted, behaviour-neutral, into one function
   with two callers: the batch and a new `EraseGuestSession`. The new use case reads that one session
   with its file keys (`ExpiredGuestDataPort.session_with_files`, the same key collection as
   `list_expired`, by id, **with no expiry predicate**) and runs the step. Rows committed, then files
   (ADR-0006 §2); files unlinked only if **this call's** `DELETE` removed the session (ADR-0018 (a)),
   which also closes the erase's races with a claim and with a concurrent purge — the loser reads
   404, honestly. Unlink failures are returned (200 with `files_unlinked` and `unlink_failures`); a
   refused `DELETE` escapes as 503 with nothing unlinked. An erase is allowed while a run is in
   flight, as the purge and `EraseAccount` allow it: the worker already handles a vanished run and
   the sweep a late file.
2. **The admin purge and sweep are entry points, like the CLI and the task.** They construct the same
   use cases with the same arguments. A **preview is a `GET`** (dry run: no lock, no heartbeat, no
   write) and a **run is a `POST`**, so a prefetch, a retry or a crawler can never delete. The purge
   run takes the same lock with the same TTL: **held → 409 `purge_in_progress`, nothing done**, never
   a 200 (a run that did nothing must not read as a success — the CLI's exit-3 rule); unreachable →
   it runs anyway and says so (ADR-0018 decision 6, fail open); success writes the heartbeat. One
   request is **one batch of at most 100**, the CLI's `--limit` path, with `overdue` after the run in
   the answer so the operator clicks again. The sweep takes no lock, uses the default grace only, and
   **fails closed**: 503, nothing unlinked, when the database cross-check cannot run.
3. **A guest session's data is read through the guest's own use cases with `GuestOwner(<path
   session_id>)`** — ADR-0034's pattern, extended by ADR-0008 (l). The use case's equality
   authorizes each row against the session; a gone or expired session is **404
   `guest_session_not_found`**, translated locally, never the global 401 (on a bearer route that
   would trip the client's refresh-on-401). A guest CV's admin shape is `BaseCvResponse` plus
   `extracted_text`; bodies are plain text, as in 4.3.
4. **Live guest sessions are a read model behind a query port** (`GuestSessionDirectoryQuery`): live
   only (`expires_at > now`; expired ones are the purge's backlog, counted by `overdue`), keyset on
   `(expires_at, id)` descending over the existing index, four counts by correlated subqueries, and
   `token_hash` never selected.
5. **Storage is measured, never read.** `SummarizeStorage` runs the orphan sweep's existing threaded
   walk — sizes come from the `lstat` it already holds (`ScannedFile.size_bytes`) — and one
   `ExpiredGuestDataPort.owners_of(keys)` lookup. The answer is five buckets of counts and bytes:
   guest, user, unreferenced, partial, unrecognized. No file is opened, no `du` is run, and it is
   computed per request, not cached: there is no beat entry, no Redis key and nothing to go stale.
6. **Job health is `/health/ready`'s facts, rendered in the admin page.** No new endpoint; the client
   reads the existing `jobs` members through the hook it already polls.

The lock, the heartbeat and the status codes belong to the entry point (ADR-0018 decision 8): the
route owns them as the CLI and the task do. The CLI is not refactored to share the route's code;
the one shared piece is the per-failure log speller. Every admin retention request logs one
`admin.retention_action` line, and guest reads log 4.3's `admin.content_access` line with a guest
target — ids, counts and closed words only. No rate limit, no `Origin` check, no guest cookie read.

## Alternatives

- **An `only=` mode on `PurgeExpiredGuestSessions`** that drops its expiry predicate for one id.
  Rejected (OQ-2): "purge" would mean two things, and the batch's dry-run, limit and report would
  carry a mode the batch never needs. Lifting the step gives one implementation of "delete a session
  and its files" with two honest names.
- **A new `DELETE` for /admin.** Rejected by the owner's Phase 4 decision: a second deletion path
  that must agree with the purge's for ever, including ADR-0018 (a)'s boolean.
- **Refusing an erase while a run is in flight** (4.3's history-delete 409). Rejected (OQ-3): the
  erase is the "remove it now" tool; the refusal would need a new port read and its own race with a
  run being queued, for a case the worker and the sweep already handle.
- **423 Locked for a held lock.** Rejected (OQ-6): WebDAV's lock-token semantics, which no client
  here speaks; 409 is this codebase's "another operation is in progress" (`tailoring_run_in_progress`,
  `refresh_in_progress`).
- **The CLI's full purge loop in one request.** Rejected (OQ-5): it needs a deadline nobody else uses
  and a `complete: false` answer; one bounded batch per click is the CLI's `--limit` path.
- **Caching the storage overview** (beat, Redis). Rejected (OQ-8): one walk at page open is within
  budget at 10 000 files, and a cache is a second number that can disagree with the volume.
- **Reading file contents, or `du`.** Rejected: attribution needs a key and a row, not bytes; reading
  a file is reading a CV.
- **Metadata-only guest views** (counts, or lists without bodies). Rejected (OQ-1): parity with 4.3 and
  PRD §3's Admin row; the guest is already told the operator can read stored data, and every handler
  body already exists.
- **An admin health endpoint.** Rejected (OQ-10): `/health/ready` already reports the jobs as facts
  (ADR-0019), and a second endpoint is a second place for them to disagree.

## Consequences

- **Easy:** no new deletion code. A fix to the purge's step reaches the batch, the task, the CLI and
  the admin erase at once. The guest read routes are routes, not logic.
- **Hard:** the purge now has three entry points that each own the lock, the heartbeat and the
  refusal codes; a fourth must do the same. The global `GuestSessionNotFound` → 401 mapping means
  every guest handler under `/api/admin` translates it locally, and one that forgets signs the admin
  out.
- **Privacy:** guest data may now go **before** 24 hours, by an operator's explicit click — never
  after (ADR-0006 note). A guest CV's text sits in the admin's tab memory until `gcTime`, sign-out or
  the session's erasure; every admin answer is `no-store`. The storage page shows counts and bytes,
  never a key or a filename.
- **Operations:** an admin purge run on production is the rehearsal ADR-0018 decision 5 owes before
  the schedule is turned on; turning it on (`.env`, then `up -d api worker beat`) stays a separate,
  deliberate step.
- **Watch:** the walk's time as the volume grows (re-measure past 10 000 files before adding a
  cache); a path-built `GuestOwner` outside `admin_guests.py` (the AST scan fails); a purge `POST`
  answering 200 while the lock was held.
