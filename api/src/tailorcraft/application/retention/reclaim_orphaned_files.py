"""The `ReclaimOrphanedFiles` use case: the recovery for a file that outlived the row that named it.

Read it next to `purge_expired_guest_sessions.py`, its sibling in this package. That one works from
the **rows** outward — it asks the store of record what is overdue and unlinks what those rows name.
This one works from the **volume** inward, which is the only direction that can see the survivor of
R-4 (an unlink that failed after the row was committed away), R-6 (a SIGKILL inside the chosen crash
window), and R-21 (a worker writing a rendered file under a derived key after its row was purged).
An orphan is invisible to every row-driven query by definition: there is no row left to join from.
So there is no scheduled recovery either — this sweep is operator-run, never on beat (ADR-0018
decision 9's wiring table), and `purge-guests --orphans` is the one command that runs it.

**SKELETON step.** `__init__` is fully written and really stores its arguments, so `qa`'s T12 RED
tests fail on their *assertions* rather than on a `TypeError` from the constructor or an
`ImportError` from the module — an `ImportError` red proves a file is absent, not that the assertion
discriminates (docs/sdlc.md §2). Only `__call__`'s body is deferred; its signature and return type
are real, and T13 fills in the five steps its docstring specifies.

**This layer does not log**, the same house rule `render_export_job.py`'s module comment writes out
in full and `purge_expired_guest_sessions.py` repeats. `retention` publishes no domain event either
(ADR-0018 decision 9), so the returned `OrphanScanReport` is this module's **only** output channel.
That matters more here than it does for the purge: the one thing this sweep touches that could carry
a person's name is a filename nobody in this system wrote (R-37), and a second output channel opened
here would emit records the privacy test cannot see.
"""

from __future__ import annotations

from datetime import timedelta

from tailorcraft.domain.retention.errors import OrphanScanAborted
from tailorcraft.domain.retention.ports import ExpiredGuestDataPort, OrphanFileScannerPort
from tailorcraft.domain.retention.value_objects import OrphanScanReport, RetentionWindow
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.files import FileRef, FileStorePort, FileStoreUnavailable


class ReclaimOrphanedFiles:
    """Unlink the files on the volume that no row names and that are old enough to be sure of
    (AC-22, AC-23, AC-24; ADR-0018 decision 6).

    **No command dataclass**, for the reason `PurgeExpiredGuestSessions` and `AbandonStaleExportJobs`
    both state: a use case takes a frozen input dataclass when a caller names *what* to act on. This
    one names nothing — it acts on whatever the volume holds now, and its four bounds (the window,
    the grace period, the limit and the dry-run flag) are configuration fixed at construction.

    **`limit` is `int | None` here, and the purge's `batch_limit: int` is not — the difference is
    deliberate rather than drift.** The purge is a *scheduled, per-tick batch*: a bound on it is a
    bound on how much work one tick does, and whatever it leaves is picked up by the next tick an
    hour later, so "no limit" is a meaningless setting and the default is a number. This sweep has
    no next tick. It is an operator running one command over a whole volume, and the honest default
    for "how much of the volume should I consider" is **all of it** — `None`. A limit here is an
    operator deliberately taking a small bite first (the runbook's `make purge limit=50` habit,
    applied to orphans), not a scheduler's throttle, and the type says which of the two it is.

    **This fails CLOSED, and the purge lock fails OPEN, and those are the same design decision
    pointing in two directions** (ADR-0018 decision 6). Both halves are written here, at the point
    of the contradiction, because a reader who meets only one of them will "make them consistent"
    and will pick the wrong one half the time:

    - The purge's Redis lock **fails open**: if Redis is unreachable the purge runs anyway. The
      worst thing an absent lock can cost is *duplicated work on an idempotent job* — two runs
      deleting the same already-deleted session, which the port's no-op contract and `delete`'s
      `missing_ok` contract both absorb. The worst thing a *skipped* purge costs is a broken privacy
      promise (FR-6), and PII sitting on disk past its window is not recoverable by apologising.
    - This cross-check **fails closed**: if the database cannot answer, nothing is unlinked and the
      sweep raises `OrphanScanAborted`. The worst thing an absent cross-check can cost is *a file*,
      irreversibly — somebody's CV, or the export they were about to download. **Deleting a file
      because we could not ask whether it is referenced is the one irreversible mistake this tool
      can make.**

    The rule underneath both is one sentence: **put a mechanism's failure on the side whose loss is
    recoverable.** Duplicated work in one case; a file in the other. Applying one direction to both
    would either stop purges when Redis blinks or delete a stranger's CV when Postgres does.

    **Why the cross-check exists at all, against ADR-0011 §4's "no query at all".** §4 is quoted
    often enough to be worth answering directly, because it reads like a licence to skip step 3.
    Its claim is about the **layout**: a `FileRef` key is a UUIDv7, a UUIDv7 carries its own
    timestamp, and therefore the *scan* — "what is on this volume and how old is it" — needs no
    database. That is true, and this use case depends on it: `OrphanFileScannerPort` asks Postgres
    nothing. It is not a licence to **delete** on age. Two independent reasons:

    - Phase 2.2 will put files on this volume that are older than any retention window and must live
      for ever (a registered user's stored base CV is not guest data and no window applies to it).
      An age-only rule deletes those the first time it runs.
    - Even today, age and orphanhood are different claims. A delayed beat plus a long-lived session
      — or a session created just before its window was widened — leaves a file older than the floor
      whose row is alive and whose owner can still click download.

    **The scan is database-free; the deletion is not.** Step 2 is the part §4 licenses and step 3 is
    the part it does not.

    **An unrecognised file is counted and never deleted** (R-37, AC-24). `ScannedFile.ref` is
    `FileRef | None` and there is no name field at all, so the adapter hands back `ref=None` and the
    filename never leaves it — which means this use case *cannot* log the name even if a future
    entry point tries. It is skipped rather than deleted because the one thing on that volume whose
    name might be a person's name is exactly the file this system cannot explain: nobody here wrote
    it, so nobody here knows what losing it costs. A non-zero `unrecognized` is worth a human look,
    which is precisely the thing a count can prompt and a deletion cannot undo.

    **No `except Exception` floor, for R-15's reason, which applies here word for word.** A *port*
    promises to translate every failure into the domain's language and therefore needs a catch-all
    underneath its named translations (ADR-0012 obligation 10). A **use case promises no such
    thing**, and a floor around a job that *deletes things* would turn any bug into a green exit code
    and a tidy report of zeroes. Exactly two failures are caught, and neither is a floor:

    - the **cross-check** (step 3), caught and re-raised as `OrphanScanAborted` so the sweep's
      fail-closed contract is a type rather than a comment — and re-raised, not swallowed, so the
      CLI exits 1 and the operator learns the check did not run instead of reading zero reclaimed
      files as a clean volume;
    - a **per-file unlink failure** (step 4), counted into `failed` and the sweep continues. A
      failure that is counted is not a failure that is swallowed, and one unreadable file must not
      abandon the rest of the volume.

    Everything else escapes by design (R-15). `Exception`, never `BaseException`: a cancelled sweep
    must still cancel.
    """

    def __init__(
        self,
        scanner: OrphanFileScannerPort,
        data: ExpiredGuestDataPort,
        files: FileStorePort,
        clock: Clock,
        window: RetentionWindow,
        grace: timedelta,
        limit: int | None = None,
        dry_run: bool = False,
    ) -> None:
        self._scanner = scanner
        self._data = data
        self._files = files
        self._clock = clock
        self._window = window
        self._grace = grace
        self._limit = limit
        self._dry_run = dry_run

    async def __call__(self) -> OrphanScanReport:
        """Run one orphan sweep and report it in counts. T13 implements these five steps.

        1. ``now = self._clock.now()`` — one instant for the whole sweep, from the `Clock` port and
           never `datetime.now()`, so a run cannot disagree with itself about when "now" was.
           ``cutoff = now - (timedelta(hours=self._window.hours) + self._grace)``.

           **The floor is the retention window *plus* a grace period** (default 24 h of grace on top
           of a 24 h window, so 48 h in total), and the addition is the argument rather than a
           safety-margin reflex. *"The file is older than the retention window"* and *"no row points
           at this file"* are **different claims**, and only the second one licenses an unlink. A
           file that is merely older than the window may belong to a session that has not expired
           yet — the window is frozen into `expires_at` at `GuestSession.start`, so a session
           started before a settings change, or one whose purge tick was delayed, can own a file
           older than `hours` while its owner is still clicking around the workspace. The grace
           period is the margin in which that disagreement resolves itself: by the time a file is
           `hours + grace` old, any session that owns it is not merely expired but has survived at
           least one scheduled purge, and the cross-check in step 3 is what actually decides.

           Note that this computes the floor from ``self._window.hours`` **directly**, and does not
           call `RetentionWindow.expiry_cutoff`. That is not an oversight and the two are not
           interchangeable: `expiry_cutoff` returns `now` unchanged on purpose (its docstring says
           why at length — the window was already applied once, at `GuestSession.start`, and
           subtracting it again would silently double the retention promise). This sweep is asking a
           genuinely different question — *how old is old enough to consider a file at all* — which
           is the one question in the codebase that really does subtract `hours`. `hours` exists on
           that value object partly for this caller.

        2. ``scanned = await self._scanner.scan_older_than(cutoff)`` — the directory walk, which the
           adapter performs **in a thread** (AC-42, ADR-0011 §5): a walk of a real volume on the
           event loop blocks every concurrent user, and this is the slice's one new filesystem
           traversal. ``self._limit``, when it is not `None`, bounds how many of those entries this
           run considers. Entries come back as `ScannedFile` values — `ref`, `created_at`,
           `is_partial` — and **no name** (R-37).

           Not wrapped in anything (R-15): a scan that cannot read the volume means the sweep could
           not find out what is there, and that must fail the command rather than return a clean-
           looking report of zeroes.

        3. **Cross-check against the database**: ``await self._data.which_are_referenced(keys)``,
           one batched query over the keys step 2 recognised — not `len(keys)` round trips, and not
           a question asked per file inside the loop. It returns the **referenced** set, so step 4
           deletes what is *not* in the answer; an adapter bug that returns too few keys can only
           spare files, never delete them.

           **If it raises, the whole sweep aborts with `OrphanScanAborted` and nothing at all is
           deleted** (R-33, AC-23). This is the one catch in the module, and it re-raises rather
           than counting: there is no partial answer to "which of these are referenced" that would
           make unlinking any subset safe, so a failure here is a fact about the whole run. The CLI
           exits 1 on it. See the class docstring for why this direction is the opposite of the
           purge lock's, and why both are right.

        4. **Unlink only two kinds of file, each of them older than the cutoff by step 2's
           construction:**

           - a **`.part` file** (``is_partial``): nothing ever references one — a `.part` is
             `LocalFileStore`'s write-then-rename temporary (ADR-0011 §5), so a `.part` that
             outlived the floor is by definition a write that never completed and no row can name it
             under that name. No cross-check is needed for these, which is why `is_partial` is a
             flag on the element rather than a second port method (R-36).
           - a **recognised file** (``ref is not None``) that the step-3 answer does **not** contain
             — the actual orphan.

           Everything else is skipped and counted, and each count is a rule holding rather than
           noise: ``too_young`` (R-34 — an entry the adapter returned that is not past the floor),
           ``referenced`` (R-35 — the cross-check doing its job), ``unrecognized`` (R-37 — a name
           that is not a valid `FileRef` key: counted, never named, **never deleted**; see the class
           docstring).

           ``await self._files.delete(ref)`` per orphan, counting ``reclaimed``. A failure counts
           into ``failed`` and the sweep continues to the next entry (R-38's shape, one level up
           from the scanner's own unreadable-subtree count): one file the volume will not give up
           must not abandon the rest of it. As in the purge, ``reclaimed`` means *keys we asked the
           store to remove* — `FileStorePort.delete` is `missing_ok` by contract, so nothing here
           can honestly claim the bytes were there (R-5).

           **If ``self._dry_run``, nothing in this step unlinks anything** (R-39, AC-22): every
           count is computed exactly as it would be for a real run — which is the point, since the
           report is what the operator reads before authorising the real one — but no
           `files.delete` call is made at all. The *absence* of the side effect is the acceptance
           criterion, and ``dry_run`` is a **field** on the report rather than something the caller
           is trusted to remember, so this report can never be read as a record of deletions that
           never happened.

        5. Return the `OrphanScanReport`: ``scanned``, ``referenced``, ``too_young``,
           ``unrecognized``, ``reclaimed``, ``failed``, ``dry_run``. It is the only output — this
           layer does not log, and the entry point builds its single line from these counts.

        Nothing else is caught (R-15). An unexpected exception escapes, the CLI exits 1, and whatever
        had already been unlinked stays unlinked — which is safe, because everything this sweep
        unlinks was already unreferenced when it asked.
        """
        # Step 1. One instant for the whole sweep, from the `Clock` port (never `datetime.now()`),
        # so a run cannot disagree with itself about when "now" was.
        now = self._clock.now()

        # **The floor is computed from `self._window.hours` DIRECTLY, and `RetentionWindow.
        # expiry_cutoff` is deliberately not called here.** This is the single caller in the
        # codebase that genuinely subtracts the window, and `expiry_cutoff` is the method whose
        # *name* sounds right for it — which is exactly why this comment is at the line rather
        # than only in the docstring above. `expiry_cutoff` returns `now` **unchanged**, on
        # purpose (its own docstring argues it at length: the window was already applied once, at
        # `GuestSession.start`, and subtracting it a second time would silently double the
        # retention promise). Reaching for it here would make this floor `now` itself, every file
        # on the volume would be "old enough", and the sweep would offer the entire volume up to
        # the cross-check — one database hiccup away from reclaiming a live user's CV. The window
        # *plus* the grace period is the margin in which "older than the window" and "nothing
        # references it" have had time to stop disagreeing; step 3 is what actually decides.
        cutoff = now - (timedelta(hours=self._window.hours) + self._grace)

        # Step 2. The directory walk, which the adapter performs in a thread (AC-42). **Not
        # wrapped in anything** (R-15): a scan that cannot read the volume means the sweep never
        # found out what is there, and that must fail the command rather than return a clean-
        # looking report of zeroes.
        scanned = await self._scanner.scan_older_than(cutoff)

        # `limit` bounds how many of those entries *this run considers*, applied here rather than
        # pushed into the port: `scan_older_than` asks a question about the volume ("what is older
        # than this?") and the answer to that question does not depend on how big a bite an
        # operator chose to take. `None` — the honest default for a sweep with no next tick — is
        # the whole volume. Everything below, `scanned` included, is a statement about `entries`,
        # so the five counts in the report always partition exactly what this run looked at.
        entries = list(scanned)[: self._limit]

        too_young = 0
        unrecognized = 0
        # Every entry that survived both filters, in scan order, carrying its `is_partial` flag —
        # the two kinds step 4 may unlink.
        considered: list[tuple[FileRef, bool]] = []

        for entry in entries:
            # The floor first, and before the `ref is None` test, because it is a property of the
            # entry rather than of what the entry turned out to be: an entry the adapter handed
            # back that is not past the floor was not considered *at all*, whatever its name looks
            # like (R-34). An unrecognised file that is also too young is therefore counted
            # `too_young` this run and `unrecognized` on a later one — it is still sitting there,
            # and the count that prompts a human look is not lost, merely deferred until the
            # sweep would actually have been willing to act.
            if entry.created_at > cutoff:
                too_young += 1
                continue
            # R-37. `ScannedFile` has no name field at all, so an unrecognised filename — the one
            # thing on this volume that might be a person's name — structurally cannot be logged,
            # reported or deleted from here. It is counted and left alone.
            if entry.ref is None:
                unrecognized += 1
                continue
            considered.append((entry.ref, entry.is_partial))

        # Step 3. **One batched cross-check, and the batch excludes `.part` files.** Nothing ever
        # references a `.part` (it is `LocalFileStore`'s write-then-rename temporary, ADR-0011
        # §5), so asking the database about one is a round trip whose answer cannot change a
        # decision — which is precisely why `is_partial` is a flag on the element rather than a
        # second port method (R-36).
        #
        # The keys are also **pre-filtered by age**, by construction of `considered` above: an
        # entry we have already decided not to touch is not made safer by asking the database
        # about it, and the narrowest question is the one whose failure has the smallest blast
        # radius — this call failing aborts the whole sweep (below), so it should be asked about
        # as few keys as can possibly matter.
        referenced_keys: frozenset[FileRef] = frozenset()
        keys = [ref for ref, is_partial in considered if not is_partial]
        if keys:
            try:
                referenced_keys = await self._data.which_are_referenced(keys)
            except Exception:
                # **The one catch in this module, and it re-raises** (R-33, AC-23). It is
                # broad for the reason the purge's R-3 catch is broad: `which_are_referenced`
                # documents no failure type, and an allow-list would be a bet that we named
                # every way a driver can refuse a read — losing that bet here means deleting
                # files because we could not ask whether anything points at them, which is
                # the one irreversible mistake this tool can make. It is **not** the floor
                # R-15 forbids: a floor wraps the whole run and reports zeroes, while this is
                # scoped to one `await` and converts it into a type the CLI exits 1 on.
                # `Exception`, never `BaseException`: a cancelled sweep must still cancel.
                #
                # **`from None`, not `from exc`** — the codebase's convention wherever the
                # original could quote data (`rate_limit.py`, `posting/fetching.py`, the two
                # repositories, `llm/parsing.py`). A failed read carries its data out through
                # three layers, and the `raise … from` chain is the third: asyncpg quotes
                # values and PostgreSQL's `DETAIL: Failing row contains (…)` is the whole row
                # — here, rows of a CV-owning table, keyed by storage keys. Cutting the chain
                # keeps that out of anything Celery or Sentry renders. The entry point logs
                # the count of keys it never got an answer for, which it already has.
                raise OrphanScanAborted() from None

        # Step 4. In scan order, so the report and the volume agree about which entries a
        # `--limit` run acted on.
        referenced = 0
        reclaimed = 0
        failed = 0

        for ref, is_partial in considered:
            # A `.part` skips this test entirely; it was never in the batch above, so it could not
            # be in the answer. The sweep deletes what is **not** in the referenced set, which is
            # why the port returns that direction: an adapter bug that returns too few keys can
            # only spare files, never delete them (R-35).
            if not is_partial and ref in referenced_keys:
                referenced += 1
                continue

            # R-39, AC-22. Every count above and below is computed exactly as it would be for a
            # real run — that identity is the whole point, since this report is what an operator
            # reads before authorising the real one — but no `files.delete` call is made at all.
            # The *absence* of the side effect is the acceptance criterion, so the branch sits at
            # the single call site rather than being trusted to a caller.
            if self._dry_run:
                reclaimed += 1
                continue

            try:
                # **A partial and a final file are two different paths, so they are two different
                # calls** (T18b). `FileRef`'s grammar cannot express a `.part` name, so a partial
                # reaches here as *the base ref plus a flag* — and calling plain `delete(ref)` for
                # both unlinked the **final** key: the `.part` survived and was reported reclaimed,
                # and where a live file sat at that key it was deleted, with the cross-check
                # structurally unable to save it because partials are excluded from it by design.
                # The flag decides the method here, once, rather than being threaded into one call
                # as a boolean a caller can point the wrong way.
                if is_partial:
                    await self._files.delete_partial(ref)
                else:
                    await self._files.delete(ref)
            except FileStoreUnavailable:
                # R-38. Counted, not swallowed, and the sweep continues: one file the volume will
                # not give up must not abandon the rest of it.
                failed += 1
                continue
            # As in the purge, `reclaimed` means *keys we asked the store to remove* — `delete` is
            # `missing_ok` by contract, so nothing here can honestly claim the bytes were there
            # (R-5).
            reclaimed += 1

        # Step 5. The report is this layer's only output: `retention` publishes no domain event
        # and this layer does not log.
        return OrphanScanReport(
            scanned=len(entries),
            referenced=referenced,
            too_young=too_young,
            unrecognized=unrecognized,
            reclaimed=reclaimed,
            failed=failed,
            dry_run=self._dry_run,
        )
