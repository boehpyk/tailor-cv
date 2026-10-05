"""`tailorcraft.cli erase-account --user-id <uuid> [--dry-run]` — the operator's account erasure
(slice 2.2, AC-31, S-47), and its own composition root.

The self-service route (`POST /api/auth/delete-account`) asks for the password. This command does
not: **operator authority**, and — with no password reset until an email channel exists — the only
way out for a user who has forgotten theirs (technical plan §0.5). It replaces 2.1's runbook line
"delete the `identity_user` row", which became **wrong** the moment an account owned files: a bare
`DELETE` cascades every row and orphans every file.

**It is thin**, exactly like `purge-guests` and `revoke-logins`: parse, call `EraseAccount` — the
same use case the route calls, not a second copy of it — translate to an exit code.

| outcome | exit |
|---|---|
| erased, or the dry run found the account | **0** |
| no account with that id, the database is not the configured one, or the database failed | **1** |
| no `--user-id`, a malformed id, any other usage error | **2** — argparse's own, in `cli.py` |

**The database guard.** Before anything is read, `SELECT current_database()` must equal the
database named in `Settings.database_url`; otherwise the command refuses and deletes nothing (AC-31:
no cross-database erasure). A connection string that routes somewhere else — a pooler default, a
service file, a hand-edited URL — should be a refusal, not a surprise. The two names are printed:
they are database names, never credentials.

**Rows first, committed, then files** — the use case's order, made durable by the same
`CommittingAccountData` the route binds. A file that fails to unlink is an orphan for
`purge-guests --orphans`, reported as a count and one warning line per failure with its class name.

**Nothing printed or logged names a person**: the user id (an opaque UUIDv7), counts, class names.
Never the email, never a file key, never an exception's message or `exc_info` — a driver error
quotes the row it refused (CLAUDE.md, "A failed database write carries its data out through three
layers"). **One log line per run**, plus S-45's one line per failed unlink.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tailorcraft.application.retention.erase_account import EraseAccount
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.retention.errors import AccountNotFound
from tailorcraft.domain.retention.value_objects import AccountCounts, AccountErasureReport
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.persistence.database import create_engine, create_session_factory
from tailorcraft.infrastructure.persistence.registry import configure_mappings
from tailorcraft.infrastructure.retention.data_access import CommittingAccountData
from tailorcraft.infrastructure.settings import Settings, get_settings

log = structlog.get_logger(__name__)

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1

EVENT_ACCOUNT_ERASED: Final = "retention.account_erased"
EVENT_ACCOUNT_FILE_UNLINK_FAILED: Final = "retention.account_file_unlink_failed"
EVENT_ERASE_ACCOUNT_DRY_RUN: Final = "retention.account_erasure_dry_run"
EVENT_ERASE_ACCOUNT_REFUSED: Final = "retention.account_erasure_refused"
EVENT_ERASE_ACCOUNT_FAILED: Final = "retention.account_erasure_failed"


class _ForeignDatabase(Exception):
    """The connection landed in a database other than the one `Settings.database_url` names."""

    def __init__(self, expected: str | None, actual: str) -> None:
        super().__init__("connected to an unexpected database")
        self.expected = expected
        self.actual = actual


def run_from_cli(*, user_id: UUID, dry_run: bool) -> int:
    """`tailorcraft.cli erase-account`. Returns an exit code; raises nothing an operator sees.

    Settings are read here and passed down (`os.environ` is read in exactly one place); logs go to
    **stderr** so stdout carries the one-line report and nothing else — `purge-guests`'s reason.
    Sentry is not initialised: an operator at a terminal, not a service.
    """
    settings = get_settings()
    configure_logging(settings)
    _route_logs_to_stderr()
    return asyncio.run(erase_account(settings, user_id=UserId(user_id), dry_run=dry_run))


async def erase_account(settings: Settings, *, user_id: UserId, dry_run: bool) -> int:
    """Erase `user_id` against `settings.database_url` (or count, on a dry run), print one line, and
    return the exit code. The seam a test drives with a `Settings` it built — `get_settings()` under
    `APP_ENV=test` still names the **dev** database, and this command deletes.

    **The engine is built here, inside the loop `asyncio.run` opened, and disposed before it
    closes** (an asyncpg connection is bound to the loop that created it); `configure_mappings()`
    first, as every composition root does.

    **A dry run commits nothing** — it rolls its read transaction back — so "deletes nothing" is a
    property of this branch, not only of `count_account`.
    """
    started_at = time.monotonic()
    configure_mappings()
    engine: AsyncEngine | None = None
    counts: AccountCounts | None = None
    report: AccountErasureReport | None = None
    try:
        # Inside the `try`: a malformed `DATABASE_URL` raises here, and is exit 1 like any other
        # database failure rather than a traceback.
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        async with factory() as session:
            try:
                await _refuse_a_foreign_database(session, settings)
                # Deferred import, for the mapper-configuration reason `deps.get_account_data`
                # documents: the adapter reads mapped attributes at import.
                from tailorcraft.infrastructure.persistence.retention.account_data import (
                    SqlAlchemyAccountData,
                )

                accounts = CommittingAccountData(SqlAlchemyAccountData(session), session)
                if dry_run:
                    counts = await accounts.count_account(user_id)
                    await session.rollback()
                else:
                    erase = EraseAccount(accounts, LocalFileStore(settings.upload_dir))
                    report = await erase(user_id)
            except Exception:
                await session.rollback()
                raise
    except _ForeignDatabase as exc:
        log.warning(
            EVENT_ERASE_ACCOUNT_REFUSED,
            reason="foreign_database",
            user_id=str(user_id.value),
            dry_run=dry_run,
        )
        print(
            f"erase-account: refused — connected to database {exc.actual!r}, but DATABASE_URL "
            f"names {exc.expected!r}. Nothing was read or deleted.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    except AccountNotFound:
        return _no_such_account(user_id, dry_run=dry_run, started_at=started_at)
    except Exception as exc:
        # The type, never the message and never `exc_info`: see the module docstring.
        log.warning(
            EVENT_ERASE_ACCOUNT_FAILED,
            error_type=type(exc).__name__,
            user_id=str(user_id.value),
            dry_run=dry_run,
            duration_ms=_elapsed_ms(started_at),
        )
        print(f"erase-account: failed ({type(exc).__name__}).", file=sys.stderr)
        return EXIT_FAILED
    finally:
        if engine is not None:
            await engine.dispose()

    if dry_run:
        if counts is None:
            return _no_such_account(user_id, dry_run=dry_run, started_at=started_at)
        log.info(
            EVENT_ERASE_ACCOUNT_DRY_RUN,
            user_id=str(user_id.value),
            base_cvs=counts.base_cvs,
            files=counts.files,
            logins=counts.logins,
            tailoring_runs=counts.tailoring_runs,
            job_postings=counts.job_postings,
            export_jobs=counts.export_jobs,
            tracked_applications=counts.tracked_applications,
            duration_ms=_elapsed_ms(started_at),
        )
        # Slice 2.3 (AC-36): the account's history, appended to the same one line so the 2.2 prefix
        # an operator (or a script) already reads is unchanged. `files` has counted the history's
        # derived export files beside the saved CVs' since 2.3 (`count_account`). Slice 3.1 (AC-28)
        # appends the board the same way, after the history segment, so 2.2's and 2.3's prefix is
        # byte-identical.
        print(
            f"would erase account {user_id.value}: {counts.base_cvs} saved CV(s), "
            f"{counts.files} file(s), {counts.logins} login(s) (dry run); history: "
            f"{counts.tailoring_runs} tailoring run(s), {counts.job_postings} job posting(s), "
            f"{counts.export_jobs} export job(s); tracking: "
            f"{counts.tracked_applications} tracked application(s)"
        )
        return EXIT_OK

    # `report` is set on every non-dry path that reached here; the assert states it for mypy.
    assert report is not None
    log.info(
        EVENT_ACCOUNT_ERASED,
        user_id=str(user_id.value),
        base_cvs=report.base_cvs,
        tailoring_runs=report.tailoring_runs,
        job_postings=report.job_postings,
        export_jobs=report.export_jobs,
        files=report.files,
        files_unlinked=report.files_unlinked,
        files_failed=len(report.unlink_failures),
        tracked_applications=report.tracked_applications,
        duration_ms=_elapsed_ms(started_at),
    )
    for error_type in report.unlink_failures:
        log.warning(
            EVENT_ACCOUNT_FILE_UNLINK_FAILED, user_id=str(user_id.value), error_type=error_type
        )
    # The history counts are appended, as on the dry run, so the 2.2 prefix is unchanged. `files`
    # is every key the erasure tried to unlink — saved CVs' and derived export keys — so the two
    # numbers before it read against it. Slice 3.1 (AC-28) appends the board after the history
    # segment, so the 2.2/2.3 prefix is unchanged.
    print(
        f"erased account {user_id.value}: {report.base_cvs} saved CV(s), "
        f"{report.files_unlinked} file(s) unlinked, {len(report.unlink_failures)} failed; "
        f"history: {report.tailoring_runs} tailoring run(s), {report.job_postings} job "
        f"posting(s), {report.export_jobs} export job(s), {report.files} file(s) in all; "
        f"tracking: {report.tracked_applications} tracked application(s)"
    )
    if report.unlink_failures:
        print(
            "erase-account: some files could not be unlinked; run "
            "`python -m tailorcraft.cli purge-guests --orphans` to reclaim them.",
            file=sys.stderr,
        )
    return EXIT_OK


async def _refuse_a_foreign_database(session: AsyncSession, settings: Settings) -> None:
    """AC-31's guard: the database this session is connected to must be the one `DATABASE_URL`
    names, asked of the server rather than assumed from the string."""
    expected = make_url(settings.database_url).database
    actual = (await session.execute(text("SELECT current_database()"))).scalar_one()
    if actual != expected:
        raise _ForeignDatabase(expected, str(actual))


def _no_such_account(user_id: UserId, *, dry_run: bool, started_at: float) -> int:
    """S-47: exit 1 with a sentence naming the **id** — never an email, which this command never
    reads."""
    log.info(
        EVENT_ERASE_ACCOUNT_REFUSED,
        reason="no_such_account",
        user_id=str(user_id.value),
        dry_run=dry_run,
        duration_ms=_elapsed_ms(started_at),
    )
    print(f"erase-account: no account with id {user_id.value}.", file=sys.stderr)
    return EXIT_FAILED


def _route_logs_to_stderr() -> None:
    """stdout is the operator's report; `configure_logging` points at it. Same as `purge-guests`."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(sys.stderr)


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)
