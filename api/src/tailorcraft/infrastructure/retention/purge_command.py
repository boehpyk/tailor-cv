"""`tailorcraft.cli purge-guests` — the operator's half of FR-6, and its own composition root.

The Celery task in `infrastructure/tasks/retention.py` is the other runner of the same use case, and
the two are deliberately **not** the same code: a scheduled tick and a typed command owe their
caller different things. The task's asymmetries are argued in its docstring; this module holds the
CLI's side of each one.

| | Celery task | this module |
|---|---|---|
| lock held (R-8) | log `outcome=skipped`, return | **exit 3** — an operator who typed a command is owed the news that it did nothing |
| run failed (R-1) | log, **re-raise** so Celery marks it failed | log, **exit 1** — there is no Celery to tell |
| success, 0 deleted (R-14) | log, heartbeat, return | log, heartbeat, **exit 0** |
| `--dry-run` | impossible — beat never previews | no lock, no heartbeat, no write of any kind (R-13) |
| `--orphans` | impossible — the sweep is never on beat | the only place it can be run at all |

**It is thin** (AC-28). It takes the lock, calls a use case, writes the heartbeat, prints a report,
logs one line and translates the outcome into an exit code. What is expired, which files a session
owns, what is safe to reclaim and in what order — all of that is `application/retention/`'s, and
none of it is restated here. The batching loop (AC-19) is the one piece of control flow this file
owns, and it is an entry-point concern by construction: the use case runs **one bounded batch**
(`batch_limit`, which AC-11's amendment pinned at "no `None`"), and "keep going until the backlog
stops falling" is a property of a command an operator is waiting on, not of the batch.

**Its own composition root, for `tasks/container.py`'s stated reason and one more.** The engine is
built inside the loop `asyncio.run` opened and disposed before that loop closes, because an asyncpg
connection is bound to the loop that created it. And it is *this* file's root rather than an import
of the worker's because `tasks/container.py` imports `GeminiLlm` at its top: `purge-guests` calls no
model, and a command whose whole job is to delete a stranger's data should not load a vendor SDK to
do it. The two pieces the worker's root and this one genuinely share — the committing adapter and
the backlog reader — live in `data_access.py` and are imported by both, so the rule *"rows first,
**committed**, then files"* has exactly one definition.

**Nothing printed or logged here can name a person**, and that is the shape of the types rather than
the author's care (Constitution §8, AC-4, AC-16). `PurgeReport` and `OrphanScanReport` are counts and
a flag; `ExpiringGuestSession` has nowhere to put a CV, a filename or a path; `ScannedFile` carries
no name at all, which is why an unrecognised file is a *count* here and never a string (R-37). The
failure lines carry `error_type` and never the exception's message: a driver error quotes the row it
refused, and here the row is a guest session.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.retention.purge_expired_guest_sessions import PurgeExpiredGuestSessions
from tailorcraft.application.retention.reclaim_orphaned_files import ReclaimOrphanedFiles
from tailorcraft.domain.retention.errors import OrphanScanAborted
from tailorcraft.domain.retention.value_objects import (
    OrphanScanReport,
    PurgeReport,
    RetentionWindow,
)
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.files.orphan_scanner import LocalOrphanFileScanner
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.persistence.database import create_engine, create_session_factory
from tailorcraft.infrastructure.persistence.registry import configure_mappings
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.retention.data_access import (
    CommittingExpiredGuestDataAdapter,
    OverdueBacklog,
)
from tailorcraft.infrastructure.retention.heartbeat import RedisPurgeHeartbeat
from tailorcraft.infrastructure.retention.lock import PurgeLockHold, RedisPurgeLock
from tailorcraft.infrastructure.retention.log_events import (
    EVENT_PURGE_COMPLETED,
    EVENT_PURGE_FAILED,
    EVENT_PURGE_SKIPPED,
)
from tailorcraft.infrastructure.settings import Settings, get_settings

log = structlog.get_logger(__name__)

# AC-20's table, as four names. The numbers are the contract a `make` target, a shell loop and T24's
# tests all read, so they are written once and referred to by name everywhere below.
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2
"""argparse's own exit code. Nothing in this module returns it — `cli.py`'s parser raises
`SystemExit(2)` before a command function is ever called — and it is named here so that the four
codes can be read in one place."""
EXIT_LOCK_HELD: Final = 3
"""**The point of AC-20's table.** A run that did nothing because another run holds the lock must
not exit 0: that is the precise trap CLAUDE.md names — the job logs "skipped", exits 0, and a test
asserting a successful run passes against a run that never happened. The Celery task translates the
identical outcome as a quiet return, because a tick colliding with a long manual purge is normal
operation; an operator standing at a terminal is owed the news."""

# The orphan sweep's two lines. They are not in `log_events.py` with the purge's three, because the
# sweep has exactly one entry point — this one, never beat (ADR-0018) — so there is no second
# speller to keep in step.
_EVENT_ORPHANS_COMPLETED: Final = "retention.orphan_scan_completed"
_EVENT_ORPHANS_ABORTED: Final = "retention.orphan_scan_aborted"
_EVENT_ORPHANS_FAILED: Final = "retention.orphan_scan_failed"

_DEFAULT_BATCH_LIMIT: Final = 100
"""The unbounded run's batch size — the use case's own default, restated here because this module
loops and therefore has to name the number it loops in. A `--limit N` run uses `N` instead and runs
**exactly one** batch: "delete at most 50 sessions" (AC-18) and "keep going until the backlog is
empty" (AC-19) are two different commands, and the flag is what chooses between them."""

_LITERAL_NOTHING_DELETED: Final = "Nothing was deleted."
"""AC-17 and R-39 pin this line, and both dry runs end with it and nothing after it. It is the
sentence an operator greps for before trusting a rehearsal."""


def run_from_cli(*, orphans: bool, dry_run: bool, limit: int | None, grace_hours: int) -> int:
    """`tailorcraft.cli purge-guests`. Returns an exit code; raises nothing an operator would see.

    Settings are read here and passed down, so that `os.environ` is still read in exactly one place
    and every function below is testable against a `Settings` a test built (CLAUDE.md's warning that
    `get_settings()` under `APP_ENV=test` still returns the **dev** `database_url` is the reason
    that seam is worth having on a command that deletes things).

    Logging is configured because AC-21's line has to go somewhere, and then routed to **stderr**:
    `configure_logging` points at stdout, which is right for a container and wrong here, where
    stdout is the operator's report and `make purge.dry > rehearsal.txt` should capture the report
    and nothing else. The eval runner does the same thing for the same reason.

    Sentry is deliberately not initialised: this is an operator at a terminal, not a service.
    """
    settings = get_settings()
    configure_logging(settings)
    _route_logs_to_stderr()

    if orphans:
        return asyncio.run(
            _reclaim_orphans(settings, dry_run=dry_run, limit=limit, grace_hours=grace_hours)
        )
    return asyncio.run(_purge_guests(settings, dry_run=dry_run, limit=limit))


# ---------------------------------------------------------------------------------------------
# purge-guests
# ---------------------------------------------------------------------------------------------


async def _purge_guests(settings: Settings, *, dry_run: bool, limit: int | None) -> int:
    """Translate the purge into an exit code. The four rows of AC-20's table are the four returns.

    The dry run is split off **before** anything operational is built, and that is the acceptance
    criterion rather than an optimisation: AC-17 says it takes no lock and writes no heartbeat, and
    the honest way to say so is a path on which no Redis client exists and no Celery app is ever
    imported. A reader checking "does the dry run touch Redis?" reads one branch, not a flag
    threaded through five functions.
    """
    if dry_run:
        return await _purge_dry_run(settings, limit=limit)

    # Imported here, inside the deleting branch. `tasks/app.py` builds the Celery application at
    # import and carries two startup refusals of its own, and the TTL is the one thing this command
    # needs from it — so a dry run (the rehearsal an operator runs first, often on a box whose
    # broker settings nobody has checked) never builds one. It is still *that* constant and not a
    # local copy: one lock key, one TTL, two holders, one derivation from the hard time limit it
    # must outlast (ADR-0018 decision 7).
    from tailorcraft.infrastructure.tasks.app import PURGE_LOCK_TTL_SECONDS

    started_at = time.monotonic()
    redis = create_redis(settings.redis_url)
    try:
        lock = RedisPurgeLock(redis, PURGE_LOCK_TTL_SECONDS)
        async with lock.hold() as hold:
            if not hold.may_run:
                # R-8 only. `may_run` is false for exactly one outcome — held by another run — and
                # **`UNAVAILABLE` runs anyway** (R-7): the lock fails open, because the cost of a
                # skipped purge is a broken privacy promise and the cost of an overlap is duplicated
                # work on an idempotent job. Redis being down is therefore an exit **0** run, not an
                # exit 3 one; the lock adapter has already logged `retention.lock_unavailable`.
                log.info(EVENT_PURGE_SKIPPED, reason="lock_held")
                print(
                    "purge-guests: a purge is already running (the lock is held). "
                    "This run deleted nothing.",
                    file=sys.stderr,
                )
                return EXIT_LOCK_HELD

            try:
                run = await _purge_batches(settings, limit=limit, hold=hold)
            except Exception as exc:
                # R-1, R-15. One line with the exception's **type** — never its message and never
                # `exc_info`, because a failed database write carries the row it refused out through
                # the driver's message and the `raise ... from` chain, and that row is a guest
                # session. **No heartbeat is written**: a heartbeat says "the job last finished at",
                # and a run that died did not finish. Whatever had committed stays committed, and
                # the backlog — not a count from a run that died — is what says how much is left.
                duration_ms = _elapsed_ms(started_at)
                log.warning(
                    EVENT_PURGE_FAILED, error_type=type(exc).__name__, duration_ms=duration_ms
                )
                print(
                    f"purge-guests: the run failed ({type(exc).__name__}). "
                    "Sessions already deleted stay deleted; no heartbeat was written.",
                    file=sys.stderr,
                )
                return EXIT_FAILED

            duration_ms = _elapsed_ms(started_at)
            # Written on every completed run including the ones that deleted nothing (AC-21, R-14),
            # and never by a dry run (R-13). `record` never raises: by the time it runs the
            # irreversible work is done and succeeded, so failing the command over a bookkeeping
            # write would report a successful purge as a failure. `at` is a fresh instant from the
            # same whole-second clock — this field answers "when did it last finish".
            await RedisPurgeHeartbeat(redis).record(
                at=SystemClock().now(),
                outcome="ok",
                sessions_deleted=run.totals.sessions_deleted,
                files_unlinked=run.totals.files_unlinked,
                duration_ms=duration_ms,
            )
            _log_purge_line(run, dry_run=False, duration_ms=duration_ms)
            _print_purge_report(run, duration_ms=duration_ms)
            return EXIT_OK
    finally:
        # Closed on every path. A pool left open under a loop `asyncio.run` is about to close leaks
        # a connection, and the leak surfaces later as a Redis connection limit blamed on whatever
        # ran last.
        await redis.aclose()


async def _purge_dry_run(settings: Settings, *, limit: int | None) -> int:
    """AC-17: report what a run would take, and write nothing of any kind.

    **One batch, never a loop.** A dry run deletes nothing, so the backlog it is looping against
    never falls — "keep going until it is empty" would not terminate. What it reports is therefore
    what *this run* would take, which is exactly AC-17's wording.
    """
    started_at = time.monotonic()
    clock = SystemClock()
    try:
        async with _purge_use_case(
            settings, batch_limit=limit or _DEFAULT_BATCH_LIMIT, dry_run=True
        ) as (purge, backlog, session):
            # One count, used for both `overdue` and the log line's `overdue_after`. On a real run
            # those are two different questions asked at two different times; on a dry run nothing
            # moved between them, and taking the count twice would invite a reader to believe
            # something might have.
            overdue = await backlog.count()
            report = await purge()
            # Closes the read transaction the listing and the count opened. It is the only
            # transaction a dry run has, and it wrote nothing.
            await session.commit()
    except Exception as exc:
        duration_ms = _elapsed_ms(started_at)
        log.warning(EVENT_PURGE_FAILED, error_type=type(exc).__name__, duration_ms=duration_ms)
        print(f"purge-guests: the dry run failed ({type(exc).__name__}).", file=sys.stderr)
        return EXIT_FAILED

    duration_ms = _elapsed_ms(started_at)
    log.info(
        EVENT_PURGE_COMPLETED,
        sessions_deleted=report.sessions_deleted,
        sessions_failed=report.sessions_failed,
        files_unlinked=report.files_unlinked,
        files_failed=report.files_failed,
        examined=report.examined,
        overdue_after=overdue,
        dry_run=report.dry_run,
        duration_ms=duration_ms,
    )
    _print_dry_run_report(
        report,
        overdue=overdue,
        window_hours=settings.guest_retention_hours,
        as_of=clock.now(),
        limit=limit,
    )
    return EXIT_OK


@dataclass(slots=True)
class _Totals:
    """What a whole `purge-guests` invocation did, summed across its batches.

    **Not a `PurgeReport`, and it must not become one.** That type's field set is pinned by AC-4 and
    every field on it is a count of *one* run of the use case; this is the entry point adding up
    several, which is a different claim about a different thing. Zero defaults are safe here for the
    reason they are unsafe there: this object is constructed exactly once, with no arguments, and
    zero is the identity it accumulates from — not a value a second construction site might forget
    to supply.
    """

    batches: int = 0
    examined: int = 0
    sessions_deleted: int = 0
    sessions_failed: int = 0
    files_unlinked: int = 0
    files_failed: int = 0

    def add(self, report: PurgeReport) -> None:
        self.batches += 1
        self.examined += report.examined
        self.sessions_deleted += report.sessions_deleted
        self.sessions_failed += report.sessions_failed
        self.files_unlinked += report.files_unlinked
        self.files_failed += report.files_failed


@dataclass(frozen=True, slots=True)
class _PurgeRun:
    """One invocation's totals and the two backlog readings that bracket it."""

    totals: _Totals
    overdue_before: int
    overdue_after: int


async def _purge_batches(
    settings: Settings, *, limit: int | None, hold: PurgeLockHold
) -> _PurgeRun:
    """Run batches until there is no more progress to make, refreshing the lock between them.

    **The stop condition is `sessions_deleted == 0`, not `backlog == 0`**, and the difference is a
    termination bug avoided. An empty backlog gives `examined == 0` and stops on the first test. But
    a batch can also examine 100 sessions and delete none of them — one permanently refused `DELETE`
    is counted and skipped by design (R-3), and `list_expired` is ordered oldest-first, so the very
    same rows come back next time. Looping on "is the backlog empty?" would then spin for ever
    against a row that can never go. A batch that deleted nothing has nothing left to try.

    **`--limit N` runs exactly one batch of N** (AC-18). It is a deliberately small, explicit bite —
    the runbook's middle rehearsal step — and a limit that then looped would delete the whole
    backlog N at a time, which is the opposite of what the operator asked for.

    The lock is refreshed **between** batches (AC-19). A full-volume purge on a box with a real
    backlog can outlast a TTL sized for one Celery tick, and a lock that expires under a run still
    working is a lock that lets a second run start. `refresh` is a compare-and-refresh: it can never
    extend somebody else's.
    """
    async with _purge_use_case(
        settings, batch_limit=limit or _DEFAULT_BATCH_LIMIT, dry_run=False
    ) as (purge, backlog, session):
        overdue_before = await backlog.count()
        totals = _Totals()
        while True:
            report = await purge()
            totals.add(report)
            _print_batch_line(totals.batches, report)
            if limit is not None or report.sessions_deleted == 0:
                break
            await hold.refresh()

        # A fresh count, taken after the run rather than subtracted from `examined` — the number
        # that cannot be faked by a job that is not working (ADR-0018 decision 4). A concurrent run
        # or a session that expired during this one can move it, which is exactly why it is read
        # rather than derived.
        overdue_after = await backlog.count()
        await session.commit()
        return _PurgeRun(totals=totals, overdue_before=overdue_before, overdue_after=overdue_after)


@asynccontextmanager
async def _purge_use_case(
    settings: Settings, *, batch_limit: int, dry_run: bool
) -> AsyncIterator[tuple[PurgeExpiredGuestSessions, OverdueBacklog, AsyncSession]]:
    """The CLI's purge root: bind the four ports, yield the use case with its backlog reader.

    `tasks/container.py`'s `purge_expired_guest_sessions_use_case` is the same wiring for the
    worker, and the two are deliberately separate functions over shared parts rather than one
    function with a flag: this one takes `batch_limit` and `dry_run` from an operator's arguments,
    and the worker's takes neither because beat has no arguments to give.

    **The engine is built here, inside the loop `asyncio.run` opened, and disposed before it
    closes.** An asyncpg connection is bound to the loop that created it.

    `configure_mappings()` first: the purge's own SQL is Core, but `FileRefType` and the tables it
    names are built when the mapping modules import, and nothing else in this process has imported
    them.
    """
    configure_mappings()

    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            try:
                from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
                    SqlAlchemyExpiredGuestData,
                )

                # `ExpiredGuestDataPort` -> the committing wrapper: **one commit per deleted
                # session**, which is what makes "rows first, *committed*, then files" a statement
                # about durability. A single transaction over a batch would make a mid-batch failure
                # un-partial and would unlink the files of sessions whose rows then came back.
                data = CommittingExpiredGuestDataAdapter(
                    SqlAlchemyExpiredGuestData(session), session
                )
                clock = SystemClock()
                # The one place `settings.guest_retention_hours` becomes the type three readers
                # share. A non-positive window is refused by the value object at construction.
                window = RetentionWindow(hours=settings.guest_retention_hours)
                purge = PurgeExpiredGuestSessions(
                    data=data,
                    # `FileStorePort` -> `LocalFileStore`, the same root the API writes uploads to
                    # and the worker writes exports to (ADR-0011). `delete` is `missing_ok` by
                    # contract, which is half of why re-running this command is safe.
                    files=LocalFileStore(settings.upload_dir),
                    clock=clock,
                    window=window,
                    batch_limit=batch_limit,
                    dry_run=dry_run,
                )
                yield purge, OverdueBacklog(data, clock, window), session
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------------
# purge-guests --orphans
# ---------------------------------------------------------------------------------------------


async def _reclaim_orphans(
    settings: Settings, *, dry_run: bool, limit: int | None, grace_hours: int
) -> int:
    """AC-22 … AC-24: walk the uploads volume, cross-check it, reclaim what no row can explain.

    **No lock and no heartbeat, and neither is an oversight.** `retention:guest_purge:*` is the
    *guest purge's* operational state — `/health/ready` publishes it as `jobs.guest_purge`, and a
    sweep that wrote to it would tell an operator the purge had run when it had not. Nor does the
    sweep need the lock for correctness: racing a purge is benign in both directions, because the
    loser's unlink is `missing_ok` and a file the cross-check saw referenced is simply left for the
    next sweep.

    **It fails closed** (R-33, AC-23). `OrphanScanAborted` means the database cross-check could not
    be performed, so nothing was deleted and nothing may be: deleting a file because we could not
    ask whether a row still points at it is the one irreversible mistake this tool can make. That is
    the opposite direction from the purge lock's fail-open, on purpose, and the rule underneath both
    is one sentence — put a mechanism's failure on the side whose loss is recoverable.
    """
    started_at = time.monotonic()
    clock = SystemClock()
    grace = timedelta(hours=grace_hours)
    try:
        async with _orphan_use_case(settings, limit=limit, dry_run=dry_run, grace=grace) as (
            sweep,
            session,
        ):
            report = await sweep()
            await session.commit()
    except OrphanScanAborted as exc:
        duration_ms = _elapsed_ms(started_at)
        log.warning(_EVENT_ORPHANS_ABORTED, error_type=type(exc).__name__, duration_ms=duration_ms)
        print(
            "purge-guests --orphans: the database cross-check could not be performed, so the "
            "sweep deleted nothing. Nothing was reclaimed; run it again once the database is "
            "reachable.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    except Exception as exc:
        duration_ms = _elapsed_ms(started_at)
        log.warning(_EVENT_ORPHANS_FAILED, error_type=type(exc).__name__, duration_ms=duration_ms)
        print(f"purge-guests --orphans: the sweep failed ({type(exc).__name__}).", file=sys.stderr)
        return EXIT_FAILED

    duration_ms = _elapsed_ms(started_at)
    log.info(
        _EVENT_ORPHANS_COMPLETED,
        scanned=report.scanned,
        referenced=report.referenced,
        too_young=report.too_young,
        unrecognized=report.unrecognized,
        reclaimed=report.reclaimed,
        failed=report.failed,
        dry_run=report.dry_run,
        duration_ms=duration_ms,
    )
    _print_orphan_report(
        report,
        as_of=clock.now(),
        window_hours=settings.guest_retention_hours,
        grace_hours=grace_hours,
        limit=limit,
        duration_ms=duration_ms,
    )
    return EXIT_OK


@asynccontextmanager
async def _orphan_use_case(
    settings: Settings, *, limit: int | None, dry_run: bool, grace: timedelta
) -> AsyncIterator[tuple[ReclaimOrphanedFiles, AsyncSession]]:
    """The CLI's orphan root — the only root in the product that binds `OrphanFileScannerPort`.

    Nothing in the worker can start this sweep and nothing in the API can either (technical plan
    §3's wiring table): it walks a volume and deletes files a cross-check says nothing references,
    and a mistake there is the one irreversible loss this feature can cause. It runs where a human
    typed the command.

    **The plain adapter, not the committing wrapper**, and that is a statement rather than a
    shortcut. The sweep never calls `delete_session` — it deletes *files*, never rows — so the
    wrapper's one behaviour is unreachable from here, and wrapping a read-only use of the port in a
    per-session commit boundary would suggest to the next reader that this command deletes rows.
    `which_are_referenced` is a read whose *failure* is load-bearing (R-33), and it propagates
    untouched either way.
    """
    configure_mappings()

    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            try:
                from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
                    SqlAlchemyExpiredGuestData,
                )

                sweep = ReclaimOrphanedFiles(
                    # `OrphanFileScannerPort` -> the local scanner, which shares `LocalFileStore`'s
                    # root and puts every syscall through `asyncio.to_thread` (AC-42).
                    scanner=LocalOrphanFileScanner(settings.upload_dir),
                    data=SqlAlchemyExpiredGuestData(session),
                    files=LocalFileStore(settings.upload_dir),
                    clock=SystemClock(),
                    window=RetentionWindow(hours=settings.guest_retention_hours),
                    grace=grace,
                    limit=limit,
                    dry_run=dry_run,
                )
                yield sweep, session
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------------
# The report. Counts, instants, durations and outcomes — never a name, a key or a path.
# ---------------------------------------------------------------------------------------------


def _print_dry_run_report(
    report: PurgeReport, *, overdue: int, window_hours: int, as_of: datetime, limit: int | None
) -> None:
    """AC-17's report: the window, the instant, `overdue`, what this run would take, the files.

    **Two wordings here are load-bearing, and both were paid for elsewhere in this slice.**

    `files_unlinked` on the dry-run path is *the number of keys this run would ask the store to
    remove*. `FileStorePort.delete` is `missing_ok` by contract, so "the file is there" is not a
    fact this number carries (R-5) — and an operator reading "would delete 23 files" before a
    rehearsal will check for 23 files.

    The count is **not split into uploads and exports**, which AC-17 asks for, and the reason is a
    type rather than an omission: `ExpiringGuestSession.files` is a flat tuple of `FileRef`, and a
    `FileRef` is an opaque key with one grammar shared by both kinds — same sharding, same UUIDv7
    filename, same three extensions, one tree (ADR-0011, ADR-0016). Nothing in the value the purge
    hands upward says which kind a key is, and the extension cannot be asked: `.pdf` is both an
    uploaded CV and a rendered export. Producing the split would mean widening a type AC-4 pins or
    adding a port method for a line of console output, so the total is reported with its ambiguity
    stated. Recorded for the slice review rather than guessed at.
    """
    print("purge-guests --dry-run: nothing will be deleted.")
    print()
    print(_row("retention window", f"{window_hours} h"))
    print(_row("as of", as_of.isoformat()))
    print(_row("overdue sessions", overdue))
    print(_row("would take this run", f"{report.examined}{_of_limit(limit)}"))
    print(_row("keys it would remove", report.files_unlinked))
    print()
    print("  The key count is what this run would ask the file store to remove, not files")
    print("  confirmed present: FileStorePort.delete is missing_ok by contract.")
    print("  It is not split into uploads and exports — a storage key carries no kind, and both")
    print("  kinds share one grammar and one tree (ADR-0011). The number is the two together.")
    print()
    print(_LITERAL_NOTHING_DELETED)


def _print_batch_line(batch: int, report: PurgeReport) -> None:
    """One line per batch, so a long `make purge` shows progress rather than a silent terminal."""
    print(
        f"  batch {batch}: examined {report.examined}, deleted {report.sessions_deleted}, "
        f"failed {report.sessions_failed}; keys removed {report.files_unlinked}, "
        f"key failures {report.files_failed}"
    )


def _print_purge_report(run: _PurgeRun, *, duration_ms: int) -> None:
    """The completed run, in counts. `50 of 137` is AC-18's line and it is not an error state: a
    `--limit` smaller than the backlog is the runbook's small, explicit bite."""
    totals = run.totals
    print()
    print(_row("sessions deleted", f"{totals.sessions_deleted} of {run.overdue_before}"))
    if totals.sessions_failed:
        print(_row("sessions failed", totals.sessions_failed))
    print(_row("keys removed", totals.files_unlinked))
    if totals.files_failed:
        print(_row("key removals failed", totals.files_failed))
    print(_row("batches", totals.batches))
    print(_row("overdue now", run.overdue_after))
    print(_row("duration", f"{duration_ms} ms"))
    if totals.sessions_failed:
        print()
        print("  Some sessions could not be deleted and were left in place; the run continued past")
        print("  them by design. They are still counted in `overdue now` and the next run retries")
        print("  them. A count that never falls is the signal to investigate.")


def _print_orphan_report(
    report: OrphanScanReport,
    *,
    as_of: datetime,
    window_hours: int,
    grace_hours: int,
    limit: int | None,
    duration_ms: int,
) -> None:
    """AC-22's six counts, and one wording trap recorded at T13 so that nobody has to rediscover it.

    **`scanned` is what this run *considered*, not what is on the volume.** With `--limit N` it is
    at most N, so a label like "files on the volume" would tell an operator that a volume holding
    ten thousand files holds ten. The five other counts partition it exactly, which is the
    self-check: `referenced + too_young + unrecognized + reclaimed + failed == scanned`.
    """
    mode = " --dry-run" if report.dry_run else ""
    print(f"purge-guests --orphans{mode}:", "nothing will be deleted." if report.dry_run else "")
    print()
    print(
        _row(
            "age floor",
            f"{window_hours + grace_hours} h ({window_hours} h + {grace_hours} h grace)",
        )
    )
    print(_row("as of", as_of.isoformat()))
    print(_row("considered this run", f"{report.scanned}{_of_limit(limit)}"))
    print(_row("referenced by a row", report.referenced))
    print(_row("too young", report.too_young))
    print(_row("unrecognized", report.unrecognized))
    print(_row("reclaimed", report.reclaimed))
    print(_row("failed", report.failed))
    print(_row("duration", f"{duration_ms} ms"))
    print()
    print("  `considered this run` counts the entries this run looked at — with --limit it is at")
    print("  most that many. It is not a count of the volume.")
    if report.unrecognized:
        print("  A non-zero `unrecognized` is worth a human look: those files are never deleted")
        print("  and never named here, because a filename nobody in this system wrote may be a")
        print("  person's name.")
    if report.dry_run:
        print()
        print(_LITERAL_NOTHING_DELETED)


def _row(label: str, value: object) -> str:
    return f"  {label:<24}{value}"


def _of_limit(limit: int | None) -> str:
    """Say that a number was bounded by `--limit`, where it was."""
    return "" if limit is None else f" (limit {limit})"


def _log_purge_line(run: _PurgeRun, *, dry_run: bool, duration_ms: int) -> None:
    """AC-21's line, with the task's eight fields — every one a count, a duration or a flag.

    `duration_ms` is the **command's**, measured with a monotonic clock, not one batch's
    `PurgeReport.duration_ms`: this entry point can run several batches, and quoting one of them as
    the run's duration would be a number that shrinks as the run gets longer.
    """
    log.info(
        EVENT_PURGE_COMPLETED,
        sessions_deleted=run.totals.sessions_deleted,
        sessions_failed=run.totals.sessions_failed,
        files_unlinked=run.totals.files_unlinked,
        files_failed=run.totals.files_failed,
        examined=run.totals.examined,
        overdue_after=run.overdue_after,
        dry_run=dry_run,
        duration_ms=duration_ms,
    )


def _route_logs_to_stderr() -> None:
    """`configure_logging` writes to stdout, which is right for a container and wrong here: stdout
    is the operator's report, and `make purge.dry > rehearsal.txt` should capture the report and
    nothing else. The eval runner does the identical thing for the identical reason."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(sys.stderr)


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)
