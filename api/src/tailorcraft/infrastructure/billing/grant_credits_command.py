"""`tailorcraft.cli grant-credits --user-id <uuid> --credits N [--dry-run]` (slice 5.2, AC-30): the
operator's remedy for a run that succeeded and was still wrong (F-7), and its own composition root.

Thin, like `grant-role`, whose shape it follows: parse, call `GrantCredits`, translate to an exit code.

| outcome | exit |
|---|---|
| granted, or the dry run found the account | **0** |
| no credit account with that id, the database is not the configured one, or the database failed | **1** |
| no `--user-id`, a malformed id, `--credits` outside 1..100, any other usage error | **2** — argparse's, in `cli.py` |

**Commit, then publish**: `CreditsGranted` is logged only after `session.commit()` returns, and never
on a dry run or a failure (`role_command._PublishAfterCommit`'s reason). The foreign-database guard
runs before anything is read. Output and logs carry the user id and counts only.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.application.billing.grant_credits import (
    GrantCredits,
    GrantCreditsCommand,
    GrantCreditsResult,
)
from tailorcraft.domain.billing.errors import CreditAccountNotFound
from tailorcraft.domain.billing.value_objects import GrantAmount
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.events.logging_publisher import LoggingEventPublisher
from tailorcraft.infrastructure.identity.role_command import (
    _elapsed_ms,
    _PublishAfterCommit,
    _route_logs_to_stderr,
)
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.persistence.database import create_engine, create_session_factory
from tailorcraft.infrastructure.persistence.database_guard import (
    ForeignDatabase,
    refuse_a_foreign_database,
)
from tailorcraft.infrastructure.persistence.registry import configure_mappings
from tailorcraft.infrastructure.settings import Settings, get_settings

log = structlog.get_logger(__name__)

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1

EVENT_GRANT: Final = "billing.grant_credits"
EVENT_GRANT_FAILED: Final = "billing.grant_credits_failed"


def run_from_cli(*, user_id: UUID, credits: int, dry_run: bool) -> int:
    """`tailorcraft.cli grant-credits`. Logs to stderr; stdout carries the one-line report."""
    settings = get_settings()
    configure_logging(settings)
    _route_logs_to_stderr()
    return asyncio.run(
        grant_credits(
            settings, user_id=UserId(user_id), amount=GrantAmount(credits), dry_run=dry_run
        )
    )


async def grant_credits(
    settings: Settings, *, user_id: UserId, amount: GrantAmount, dry_run: bool
) -> int:
    """Grant against `settings.database_url` (or report, on a dry run), print one line, return the
    exit code. The seam a test drives with a `Settings` it built (`role_command.change_role`'s)."""
    started_at = time.monotonic()
    configure_mappings()
    engine: AsyncEngine | None = None
    outbox = _PublishAfterCommit()
    try:
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        async with factory() as session:
            try:
                await refuse_a_foreign_database(session, settings)
                # Deferred: the repository reads mapped attributes at import.
                from tailorcraft.infrastructure.persistence.repositories.billing.credit_account import (
                    SqlAlchemyCreditAccountRepository,
                )

                grant = GrantCredits(
                    SqlAlchemyCreditAccountRepository(session), SystemClock(), outbox
                )
                result = await grant(GrantCreditsCommand(user_id, amount, dry_run))
                if dry_run:
                    await session.rollback()
                else:
                    await session.commit()
            except Exception:
                await session.rollback()
                raise
    except ForeignDatabase as exc:
        _log_failure(exc, user_id, dry_run=dry_run, started_at=started_at)
        print(
            f"grant-credits: refused — connected to database {exc.actual!r}, but DATABASE_URL "
            f"names {exc.expected!r}. Nothing was read or changed.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    except CreditAccountNotFound as exc:
        _log_failure(exc, user_id, dry_run=dry_run, started_at=started_at)
        print(f"grant-credits: no account with id {user_id.value}.", file=sys.stderr)
        return EXIT_FAILED
    except Exception as exc:
        # The type, never the message and never `exc_info` (`role_command`'s module docstring).
        _log_failure(exc, user_id, dry_run=dry_run, started_at=started_at)
        print(f"grant-credits: failed ({type(exc).__name__}).", file=sys.stderr)
        return EXIT_FAILED
    finally:
        if engine is not None:
            await engine.dispose()

    await LoggingEventPublisher().publish(*outbox.events)
    log.info(
        EVENT_GRANT,
        user_id=str(user_id.value),
        credits=amount.value,
        balance_before=result.before,
        balance_after=result.after,
        dry_run=dry_run,
        duration_ms=_elapsed_ms(started_at),
    )
    print(_report(user_id, amount, result, dry_run=dry_run))
    return EXIT_OK


def _report(
    user_id: UserId, amount: GrantAmount, result: GrantCreditsResult, *, dry_run: bool
) -> str:
    """AC-30's two stdout lines."""
    change = f"{amount.value} credit(s), balance {result.before} → {result.after}"
    if dry_run:
        return f"would grant user {user_id.value}: {change} (dry run)"
    return f"granted user {user_id.value}: {change}"


def _log_failure(exc: Exception, user_id: UserId, *, dry_run: bool, started_at: float) -> None:
    log.warning(
        EVENT_GRANT_FAILED,
        error_type=type(exc).__name__,
        user_id=str(user_id.value),
        dry_run=dry_run,
        duration_ms=_elapsed_ms(started_at),
    )
