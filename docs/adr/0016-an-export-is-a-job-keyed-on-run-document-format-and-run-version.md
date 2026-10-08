# ADR-0016: An export is a job row keyed on run × document × format × run version; TXT and Markdown are representations, not jobs

- **Status:** Accepted
- **Date:** 2026-09-17
- **Relates to:** ADR-0005 (Celery for exports and purges — this ADR *reads* its two sentences
  rather than contradicting them), ADR-0011 (the storage key is a pure function of the id —
  **amended below**), ADR-0013 (no row for a failed fetch — the "was anything spent?" question,
  applied a second time), ADR-0014 (a run is queued and polled; §2 the row-for-every-outcome rule,
  §3 no session minted outside first contact, §5 commit-then-enqueue, §8 the context-specific
  queue port), ADR-0015 (the run's `version`, which this ADR keys a job on), ADR-0006 (guest
  retention — the cascade this ADR guarantees). Supersedes nothing.

## Context

Slice 1.5 closes the guest loop: upload → tailor → edit → **download**. FR-5 wants four formats —
`md`, `txt`, `pdf`, `docx` — from the page the user just edited on.

Three questions have to be answered before a line of it is written, and each of them is the kind
that is cheap now and expensive in 1.6 and 2.3.

1. **Is a TXT download an export job?** ADR-0005 says two things that, read literally, collide:
   *"TXT and Markdown render inline (they are string manipulation); PDF and DOCX go to a Celery
   worker"*, and separately *"an export is an `ExportJob` aggregate … that the client polls"*. One
   sentence is about where the work runs; the other is about what the resource is. They have to be
   reconciled on purpose rather than by whoever writes the router first.
2. **What happens when the user clicks *Export PDF* twice?** 1.3 answered the analogous question
   for tailoring with a **409**, because a second run is a second paid Gemini call and the user
   should be the one to choose to spend it. A second PDF render spends worker seconds for
   byte-identical output. The 409 answer is available, and it is the wrong one here — but only
   because of a difference that has to be named, or the next slice will copy the wrong precedent.
3. **What does a job render if the document changed between the click and the render?** ADR-0015
   gave the run an integer `version` that moves on every edit. A render takes seconds; a debounced
   autosave lands in under a second. The window is small and it is real.

The forces: a guest is waiting and watching a progress line; worker seconds are ours and bounded,
not money and not someone else's infrastructure; the retention job in 1.6 must be able to find
every file this slice writes, from a row or from an id alone; and nothing may put a business rule
in TypeScript.

## Decision

### 1. Two mechanisms are two resource shapes, and an inline export leaves no row

ADR-0014 §2's question — *was anything spent, and is there an artifact to own?* — decides it:

- **`md` and `txt`: no row, ever.** The render is string manipulation over a string a value object
  already validated, inside one request, producing bytes that are sent and forgotten. Nothing is
  stored, nobody waits, and the same request tomorrow gives the same answer. That is a **`GET` on a
  representation of the document**: `GET /api/tailoring-runs/{id}/documents/{kind}/download?format=txt`.
  It is the shape ADR-0013 §2a chose for a failed fetch, for the same reason — no artifact, no row.
- **`pdf` and `docx`: a row, always, including every failure.** Worker seconds are spent, a file
  lands on a volume, and the user is polling. `POST /api/tailoring-runs/{id}/exports` creates an
  `ExportJob`; `GET /api/export-jobs/{id}` polls it; `GET /api/export-jobs/{id}/file` serves its
  bytes.

So there are two resource shapes in one router, and **the shape is the rule**. `ExportFormat`
carries `delivery` (`INLINE` | `QUEUED`) as a property of the format itself, so "is this CPU-bound?"
is answered once, on the type, and never by a router, a use case or a component.

ADR-0005 is not contradicted; it is read. *"An export is an `ExportJob`"* is true of the exports
that **leave the request**, which is what that sentence was describing.

**Why job URLs are flat while the collection is nested.** The collection belongs to the run — *the
exports of this run* is the question a refreshed page asks — so `GET /api/tailoring-runs/{id}/exports`
nests, as 1.4's documents did. A job, once it exists, is its own resource with its own owner column
and its own authorization link; a nested `/tailoring-runs/{run}/exports/{id}` would make the router
check that the job belongs to the run *and* to the session — a second rule with no purpose.

### 2. The request is idempotent on (run, document, format, run version)

`RequestExport` looks up the latest job for the key. If one exists, is not `failed`, and
`was_requested_for(run.version)`, it is **returned with 200** — no new row, no second task. A
created job answers **202**. Same body either way; the status code is the honest record of whether
a row was created, and an OpenAPI reader can see both.

**Why not 1.3's 409.** A second tailoring run is a second paid call; the user should choose. A
second render of the same version produces the same bytes for worker seconds nobody paid for. There
is nothing to choose, so *here is the one you already have* is the honest answer. **The difference
is money, and that is the whole of it** — the next slice weighing a 409 against a 200 should ask
the same question in the same words.

A `failed` job does not count: a retry is a new job, as in 1.3.

This idempotency is **soft**. Two concurrent `POST`s may both miss the lookup and create two jobs
(X-23). Accepted, as 1.1's F-23 and 1.3's G-34 were: the cost is one duplicate render, bounded by
the per-session cap (40) and the 24-hour purge, and a lock on a render nobody paid for is more
machinery than the failure is worth.

**Two levels of idempotency, two mechanisms, and they are not the same question.** *Did this
message run twice?* is ADR-0005's rule, delivered by the aggregate's transition table and
`version_id_col` — keyed on the job id. *Did the user ask for the same thing twice?* is this
section — keyed on (run, document, format, run version). Conflating them is how one of them ends up
protecting nothing.

### 3. A job renders exactly the version it was requested for, or fails `source_changed`

The job stores `run_version`, the run's ADR-0015 version at request time. **Nothing textual travels
on the job**: the worker reads the document from the run at render time. Before it renders, it
compares `run.version` with `job.run_version`; a mismatch records `failed` / `source_changed` and
the UI offers *Export again*.

The alternative — render whatever is there now, and overwrite `run_version` on `mark_ready` — was
weighed and rejected twice over. It silently changes the key of a row **after** that key was used
for a lookup, and it hands the user a file whose contents they did not click for. A failure that
says *your document changed — export again* is a worse-looking outcome and a more honest one.

The window is small: a render is seconds, and the client disables every export control while a save
is pending (AC-40). `source_changed` is the backstop for the window the client cannot see.

**Staleness is `job.run_version != run.version`, computed at the API boundary**, exactly as 1.3's
`retryable` is. One column, one comparison, no content hash, and no re-derivation in TypeScript.

**The known over-approximation, accepted on purpose:** the run has one version for two documents
(ADR-0015: both tabs share one version), so editing the cover letter makes a ready CV export read
as stale. A re-render is seconds; a content digest would be a second notion of "changed" beside the
one the editor already uses; and the day that cost matters is the day ADR-0014 §9's promotion of
`TailoredDocument` to an aggregate is due anyway. **That is the trigger to watch.**

### 4. Export gets its own stale-job sweep, not a generalized one

`AbandonStaleExportJobs` is 1.3's `AbandonStaleTailoringRuns` with the aggregate swapped: beat every
60 s, `expires` 55, bounded batch, a per-job concurrency conflict counted and the batch continued.
`create_celery` refuses an `export_stale_after_seconds` at or below the hard time limit, beside
1.3's guard — and inherits the `uvicorn --workers N` never-exits footgun with it.

**Not generalized over the two aggregates.** They share a shape and not a rule: a
`StaleSweep[Aggregate]` would have to guess at the transition it calls and the reason it records.
Two thirty-line use cases are cheaper than one abstraction with a type parameter and a strategy —
the same judgement CLAUDE.md makes about a base class for two aggregates that merely share a shape.

### 5. `ExportQueuePort` is ADR-0014 §8's second data point, not its promotion

A second one-method, context-specific queue port, in `domain/export/ports.py`, with the same shape
as `TailoringQueuePort`. §8 said: *"if a third one appears with an identical signature that is the
moment to reconsider — not now, on the strength of two."* This is the two. It is recorded here so
the third one arrives to a decision already half made, and so nobody generalizes on the strength of
this ADR alone.

### 6. The job row holds no PII except by reference

`export_job` holds ids, enums, integers, instants and a file key. No name, no text, no path, no
client IP. A `SELECT *`, a failed-`UPDATE` message, a `db.dump` of this table alone or a Sentry
breadcrumb of its bound parameters discloses nothing about the person.

**That is a property, not an accident** — it is the second reason §3 refused to snapshot the
document onto the job, and it is worth defending the next time a column is proposed here.

## Amendment to ADR-0011 (the storage layout)

ADR-0011 §1 named `FileRef.for_base_cv` as the one way a key is constructed and said 1.5 would
store rendered files from the worker. It now does, and the contract is:

- **`FileRef.for_export(job_id: ExportJobId, format: ExportFormat) -> FileRef`**, beside
  `for_base_cv`, building the same `<hex[0:2]>/<hex[2:4]>/<uuid7>.<ext>` key under the **unchanged**
  grammar — `pdf` and `docx` are already admitted, and the grammar is **not widened**, because `md`
  and `txt` are never stored. `for_export` raises `ExportFormatNotQueued` for an inline format: an
  inline format has no file and therefore no ref.
- **The row column that carries it is `export_job.file_key`** (`VARCHAR(512) NULL UNIQUE`), written
  by the aggregate in `mark_ready` from its own id and format. The width is **not** chosen for this
  column: it is `FileRefType`'s, reused from `types/shared.py` where 1.1 put it and sized there *for*
  this reuse. A second decorator differing only in a length is how one storage grammar ends up
  re-validated by two different rules on the way out of two tables. (Corrected 2026-09-17 at I4 —
  this ADR first said `VARCHAR(64)`, which contradicted its own instruction to reuse the decorator.
  Nothing depends on the narrower width: a key too long for the grammar is unconstructable long
  before it reaches a column.) `mark_ready` takes no `FileRef`
  argument, so the row and the file can never disagree — neither side chose the name.
- **The retention contract 1.6 consumes** is exactly two guarantees, both tested in this slice:
  every row cascades from `identity_guest_session` (`ON DELETE CASCADE`, indexed), and
  `row.file_key == FileRef.for_export(row.id, row.format).key`. So 1.6 can read the keys of
  expiring sessions' jobs before the cascade, or reconstruct them from the ids afterwards, and
  unlink **after** the rows are gone (ADR-0006 §2's order). The filename is still a UUIDv7, so the
  orphan sweep still needs no database at all (ADR-0011 §4). Export files and base-CV files share
  the tree and the rule.
- **No repository method is added for 1.6.** A method with no consumer is a promise the code does
  not keep; 1.6 adds the one it needs.

ADR-0011's consequences now cover **output as well as input**: the uploads volume holds rendered
CVs, the `0600` and same-uid requirement applies to them, and the backup story (volume *and*
database) is restated rather than assumed.

## Note on ADR-0005

ADR-0005's *"an export is an `ExportJob` aggregate … that the client polls"* describes the exports
that **leave the request** — `pdf` and `docx`. The inline pair leaves no row; see §1 above.

## Alternatives

- **One `POST /api/exports` that answers 200-with-a-file for inline formats and 202-with-a-job for
  queued ones.** Genuinely tempting: one endpoint, one client function. Rejected on three counts — a
  `POST` for a safe, idempotent, cacheable read is the wrong verb; the client would branch on a
  status code to learn whether it holds a file or a job, which is ADR-0005's inline/queued rule
  re-implemented in TypeScript (Constitution §4.5); and the "no row" property would become a branch
  inside a handler rather than a consequence of the resource.
- **409 on a re-request, as 1.3 does.** Rejected: nothing is spent that the user should choose to
  spend. Keeping the codes identical across slices would have bought consistency at the price of
  telling the user there is a conflict when there is a file.
- **Always create a new job.** Simpler — no lookup, no cross-aggregate comparison. Rejected: a
  double click becomes two renders, the row count grows with clicks rather than with intent, and the
  UI has to pick which of two identical jobs to show.
- **Render the latest text and move `run_version` on completion.** One fewer failure row. Rejected:
  see §3 — a mutable key column and a file the user did not ask for.
- **A content hash instead of a version.** Would fix the one-version-for-two-documents
  over-approximation. Rejected for now: a second notion of "changed" beside the editor's, for a
  cost measured in seconds. Named in §3 as the thing to revisit with the aggregate promotion.
- **A `UNIQUE (tailoring_run_id, document, format, run_version)` index enforcing §2 in the
  database.** Rejected: it would turn X-23's benign race into a 500, and it would forbid the
  perfectly legitimate second job after a failure. The lookup is soft on purpose; the cap and the
  purge are the bound.
- **A generalized `StaleSweep`.** See §4.

## Consequences

- **`ExportJob` is a new aggregate in a new bounded context**, with no shared base class with
  `TailoringRun`: a job may not be requested for an inline format, and a run has no such rule.
- **`export` imports `tailoring`'s published language** — `TailoredDocumentKind`, `TailoringRunId`
  — and `tailoring` imports nothing of `export`'s. A downstream context using an upstream one's
  language is the ordinary relationship; the direction is the thing to keep.
- **`tailoring` gains no code and is never mutated by an export path.** No export use case calls
  `save` on a run. The read goes through `GetTailoringRunForSession` in the API (which carries the
  authorization rule and the 404 collapse) and through `TailoringRunRepository.find` in the worker
  (where re-checking a session that may have expired since would fail a render for a reason that
  has nothing to do with rendering — 1.3's reasoning, unchanged).
- **The per-session cap (40) and the "one current job per key" rule live in `RequestExport`, not on
  the aggregate**, because both span aggregates. Each carries a comment saying so at the point of
  contradiction.
- **`/health/ready`'s Celery probe becomes load-bearing for a user-facing path for the first time.**
  A stopped worker now presents as *Waiting for a worker…* forever (X-24), which is exactly the
  failure ADR-0009's consequence predicted.
- **The worker's two slots are shared** between `celery`, `tailoring` and `export`. Two long renders
  can occupy both while a tailoring run waits. Accepted at this scale; the separate `export` queue
  is what makes `-Q` a compose change on the day it matters, and this sentence is where that day
  starts.
- **Phase 2.2 stays additive:** `user_id UUID NULL` and a nullable `guest_session_id`; a job with no
  guest session is untouchable by the purge (ADR-0006 §3). Phase 2.3 lists this table per run.
  Phase 3.2 adds a `template` column when there is a second template, not before.

## Amendment: 2026-09-26, from the plan of slice 2.3 (`tailoring-application-history`)

Slice 2.3 lets a registered user keep tailoring runs as history and re-export them (ADR-0023). This
ADR's Consequences said 2.2 would stay additive and that 2.3 would list this table per run; both
hold. The six decisions and the ADR-0011 amendment stand. This amendment records who owns a job, how
a user's jobs are capped, how long their files live, and when that cost must be revisited.

**(a) A job's owner is its run's owner, always.** `export_job` gains ADR-0022's shape (the per-table
extension in ADR-0022's amendment (a)): a nullable `user_id`, a nullable `guest_session_id`, and the
exactly-one-owner CHECK. `ExportJob.request` takes an `owner`, and `RequestExport` passes
**`run.owner`** — never the requester, even though the requester was just authorized as that owner.
*"A job's owner is its run's owner"* spans two aggregates, so it lives in the use case with a comment
saying so, and a database-level test checks that no ownership graph crosses owners after every flow.
The worker still reads the run by id and re-checks no owner (1.3's reasoning, unchanged).

**(b) A user's jobs are capped per run, not per user.** Guests keep the per-session cap of 40. A
user-owned run has its own cap, **20 export jobs per run** (`MAX_EXPORT_JOBS_PER_USER_RUN`), soft
and in the use case like the first. A per-user cap would stop a long history from exporting at all,
where a per-run cap bounds exactly the thing §2's soft idempotency can multiply. For a user's jobs,
§2's sentence *"bounded by the per-session cap (40) and the 24-hour purge"* reads *"bounded by the
per-run cap (20)"*: no purge reaches them.

**(c) A user's export files live as long as their history entry.** A kept export is deleted by the
user's deletion of its entry, or by account erasure (ADR-0006 amendment (d)), and by nothing else.
Both find the files the way 1.6's purge does, from the row's id and format
(`row.file_key == FileRef.for_export(row.id, row.format).key` — the retention contract above), so a
`rendering` or `failed` job's already-written bytes are reached as well. The orphan sweep's
cross-check reads `export_job.file_key` for every row regardless of owner, which is what spares a
user's ready file older than any window. There is still no foreign key from `export_job` to
`tailoring_run`, and ADR-0023 records the one residual that leaves: a job inserted just after its
entry's deletion committed, with no run and no file, erased with the account.

**Re-export is idempotent, not archival.** A re-export of an unchanged run returns the job that
already exists (§2); one after an edit renders the current version. The file of an older version is
**not pruned** when a newer one is rendered, and it is not a feature either: nothing lists it.

**(d) The cost, and the trigger for pruning (OQ-15).** A rendered PDF measured about 8 KB in 2.2's
manual pass; allow up to 200 KB with fonts. A user at 500 runs with four exports each is at most
about 400 MB in the worst case, and about 16 MB typically. Pruning superseded files would be a
deletion inside a render path — machinery for a cost nobody has yet. **Trigger:** the uploads volume
above **5 GB**, or any one user above **200 MB**. Today's volume size is recorded before 2.3's
release, so the trigger is a comparison, not a guess.

## Amendment: 2026-10-08, from the plan of slice 3.2 (`export-pdf-layout-templates`)

Slice 3.2 lets a PDF be rendered in one of three layouts (ADR-0030). The six decisions and the
ADR-0011 amendment stand; the 2.3 amendment stands. This amendment records what a layout does to
"the same request", what it does not do to "out of date", and how the cap arithmetic reads now. The
title's *"keyed on run × document × format × run version"* is read with (a) below; it is annotated
here rather than rewritten, because an ADR is superseded or amended, never silently edited.

**(a) §2's key gains the layout.** `RequestExport` looks up the latest job for **(run, document,
format, layout_template, run version)**. A Modern PDF is not a Classic PDF, so asking for one when
the other exists creates a job (202); asking again for the same layout of the same version returns
the one that exists (200), exactly as before. `find_latest_for_key` compares the layout with
`IS NOT DISTINCT FROM`, so a DOCX (`NULL`) keeps working through the same method. The idempotency is
still soft (X-23's race is unchanged), and *"did this message run twice?"* is still keyed on the job
id alone.

**(b) §3's staleness does not change.** A job is stale when `job.run_version != run.version`, and
nothing else. Choosing another layout in the picker makes no file stale — it selects a different
job. An edit makes every layout's PDF stale at once, because the run has one version.

**(c) The Consequence's column, executed under another name.** *"Phase 3.2 adds a `template` column
when there is a second template"* — it does, as **`export_job.layout_template VARCHAR(32) NULL`**.
`template` alone collides with the generic word (an email template, a spec template, a Jinja
template); the column says which kind it is. It is paired with `format` by
`ck_export_job_layout_template_only_for_pdf`, `CHECK ((format = 'pdf') = (layout_template IS NOT
NULL))`, and existing PDF rows are back-filled to `classic`. No value CHECK on the ids (ADR-0030).
The downgrade refuses while any row holds a layout other than `classic`.

**(d) §6 holds.** A layout id is an enum value, not PII; the row still discloses nothing about the
person.

**(e) The caps count every layout and do not change.** 40 per guest session, 20 per user run
(amendment (b) above). Every layout of both documents plus both DOCX is 8 jobs per version, so a
user who explores everything reaches the per-run cap after two or three fully explored versions.
Trigger to raise it: `too_many_export_jobs` observed for a user in production.

**(f) The 2.3 amendment's cost arithmetic (d), restated.** That arithmetic assumed four exports per
run, one PDF and one DOCX per document: 500 runs × 4 × 200 KB ≈ 400 MB in the worst case. With three
layouts a fully explored version is eight files, so the same reading becomes 500 × 8 × 200 KB ≈
800 MB. The hard ceiling is the per-run cap, as it always was: 500 runs × 20 × 200 KB ≈ 2 GB, and
that number does not move. The pruning trigger is unchanged (the uploads volume above **5 GB**, or
any one user above **200 MB**), and the volume is re-read at 3.2's release.
