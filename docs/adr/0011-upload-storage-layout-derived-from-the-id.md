# ADR-0011: The upload storage layout is derived from the id and sweepable without the database

- **Status:** Accepted
- **Date:** 2026-09-07
- **Extends:** ADR-0006 §4 (files on a local named volume behind `FileStorePort`). Supersedes nothing.
- **Amended 2026-09-17 by [ADR-0016](./0016-an-export-is-a-job-keyed-on-run-document-format-and-run-version.md)**
  ("Amendment to ADR-0011"): `FileRef.for_export(job_id, format)` is the second constructor,
  `export_job.file_key` is the row column that carries it, and the two guarantees 1.6 consumes —
  the cascade and `file_key == for_export(id, format).key` — are named there.

## Context

ADR-0006 decided that uploaded files live on a named volume shared by `api` and `worker`, behind a
`FileStorePort` that speaks in `FileRef`s and never hands a filesystem path to the domain. It
deliberately did not specify what a `FileRef` *looks like*, because nothing needed one yet.

Slice 1.1 writes the first file, and two later slices depend on the answer:

- **1.5 (export)** stores rendered PDFs and DOCX files from the **worker**, a different container
  from the one that wrote the upload.
- **1.6 (retention)** deletes files, and — per ADR-0006 §2 — must be able to find **orphans**: files
  whose row is gone because a crash landed between the file write and the commit.

A layout is the kind of thing that is trivially chosen once and expensive to change afterwards,
because changing it means migrating bytes on disk rather than editing a column. It is a cross-slice
contract, which is what an ADR is for.

Two forces shape it. First, **a single directory does not stay small**: ext4 handles large
directories, but `ls`, backup tooling, `find` and human patience all degrade, and by then the fix
requires moving every file. Second, **the orphan sweep in 1.6 must work when the database is the
thing that failed** — a recovery tool that requires the database to identify what to reclaim is
useless in exactly the situation it exists for.

## Decision

**1. The storage key is a pure function of the aggregate id.**

```
<hex[0:2]>/<hex[2:4]>/<uuid7>.<ext>      e.g.  01/92/0192f0a1-....-....-............pdf
```

`FileRef.for_base_cv(cv_id, content_type)` builds it and is the only way one is constructed. Two
consequences follow directly, and both are the reason for the choice:

- **A retried write is idempotent.** Same id, same key, same bytes, atomic replace — no duplicate
  file, no reconciliation.
- **The row and the file can each find the other**, with no lookup table and no second column to keep
  in step.

**2. The name never derives from what the user sent.** The uploaded filename is stored in a column as
a display label and is never joined to a path. A user-supplied name is a path-traversal attempt
looking for somewhere to be concatenated; here there is nowhere.

**3. Two levels of hex sharding**, 256 directories at each level. At any plausible scale for this
product no directory holds more than a handful of files, and the shard prefix comes free from the id.

**4. The layout is sweepable without the database.** UUIDv7's leading 48 bits are the creation time in
milliseconds, so the filename *is* a timestamp. A directory walk can identify every file older than
24 hours and reclaim orphans with no query at all — which is precisely the situation ADR-0006 §2
engineered for by choosing to write the file before the row.

**5. Writes are atomic and private.** `put` creates the shard directories, writes `<key>.part`,
`fsync`s, `chmod`s to `0600`, then `os.replace`s onto the final key. A reader never observes a partial
file, and a crash mid-write leaves a `.part` the sweep can also collect. Every syscall goes through
`asyncio.to_thread` — a blocking write on the event loop is the silent failure Constitution §1 names.

**6. Containment is checked anyway, and it is the *outer* lock only.** The resolved path must be
under `root.resolve()` or the store raises. The `FileRef` grammar already makes a traversing key
unrepresentable; this is the second lock on a door that should never be reachable, and it costs one
line.

> **Amended in slice 1.6.** As originally written this resolved the **whole** path —
> `(root / ref.key).resolve()` — and `resolve()` follows symlinks, so the path handed to `unlink`,
> `open` or `read` was a planted link's **target**. Harmless while every caller passed a `FileRef`
> derived from a database row; the orphan sweep (ADR-0006 §2) is the first caller that deletes by a
> name it **discovered on disk**, which turned one pre-existing line into a deletion primitive: a
> link at a `FileRef`-shaped key clears the reference cross-check, because *the link's* key is in no
> row, and its target — a live file of another session — is destroyed while the link survives.
>
> Two changes, and the second is the load-bearing one. Containment now resolves the **parent** and
> leaves the basename un-resolved, then refuses a final-component symlink. But that is a
> **check-then-use**: the check and the syscall are two operations and a name can change meaning
> between them. So every path that opens a file does so `O_NOFOLLOW`, through one opener, and works
> on the **descriptor** — `put` and `get` alike — while `delete` and `delete_partial` rely on
> `unlink` removing a link rather than its target. `O_NOFOLLOW` also covers `<key>.part`, which the
> containment check never inspects and where a plain `open()` meant an upload wrote through a link
> and `os.replace` then installed **the link itself** at the real key.
>
> Mode-setting moved with it: `os.fchmod(fd, 0o600)` rather than `os.chmod(path, …)`, so the mode
> lands on the descriptor that was opened rather than on a name that could be re-looked-up, and is
> not subject to the umask that `O_CREAT`'s mode argument is.
>
> The threat model is honest: planting a link requires prior write access to the uploads volume, so
> this is blast-radius reduction rather than a remote exploit. It earns the lines because the damage
> is silent, irreversible, and lands on a **different** user's data.

## Alternatives

- **A flat directory of UUID filenames.** Rejected: correct until it isn't, and by the time it isn't
  the fix is moving every file.
- **Date-based sharding (`2026/09/07/<uuid>.pdf`).** Genuinely appealing — it makes "delete everything
  before this date" a directory removal. Rejected because the key stops being derivable from the id
  alone: reconstructing a path would need the row's timestamp, which is the thing that is missing in
  the orphan case. UUIDv7 gives the time ordering anyway, without the coupling.
- **Hashing the file contents (content-addressed storage).** Rejected: it buys deduplication nobody
  asked for, and makes deletion a reference-counting problem. Two guests uploading the same CV must be
  two independently deletable files — the retention promise is per session, not per byte-string.
- **The original filename, sanitized.** Rejected: sanitizing filenames is a recurring CVE genre, and
  the name is PII besides — putting it on disk spreads a person's name into directory listings,
  backups and log output.
- **Deferring the layout to slice 1.5 or 1.6.** Rejected: those slices *consume* the contract. Deciding
  it when the second consumer arrives means either they disagree or the first one migrates.

## Consequences

- **`FileRef` lives in `domain/shared/`, not in `intake`.** Export needs the identical concept in 1.5,
  and a second copy would drift. It stays pure — a validated string with a grammar, no `pathlib`, no
  I/O. The path only exists inside `LocalFileStore`.
- **The `.part` files and the orphan sweep are slice 1.6's work, but this ADR is what makes them
  possible.** 1.6 collects three things: rows past `expires_at`, files older than 24 h with no row,
  and stray `.part` files.
- **Backups must cover the uploads volume, not only the database** (restated from ADR-0006 because it
  keeps being the thing people forget). A restored row pointing at a missing file is a broken
  application with a green restore.
- **This layout assumes one filesystem, and that assumption dies first if this ever runs on two
  boxes.** `FileStorePort` is the seam: an S3 adapter uses the same key as an object key, unchanged.
  The layout was chosen so that migration is a copy, not a rename.
- `0600` means the file is readable only by the container's application user. `api` and `worker` must
  therefore run as the **same** uid — they do, from the same image, and this is now a reason not to
  change that casually.
