"""`tailorcraft.cli grant-role | revoke-role --user-id <uuid> [--dry-run]` — the only way a role
changes (slice 4.1, AC-26, AC-27, ADR-0032), and its own composition root.

**It is thin**, exactly like `erase-account`, whose skeleton it follows: parse, call `ChangeUserRole`,
translate to an exit code.

| outcome | exit |
|---|---|
| changed, already held, or the dry run found the account | **0** |
| no account with that id, the database is not the configured one, or the database failed | **1** |
| no `--user-id`, a malformed id, any other usage error | **2** — argparse's own, in `cli.py` |

Revoking the last admin, the operator's own account included, is allowed (AC-27, decision 6): the
recovery is `grant-role` over SSH, so there is no warning and no count.

**Commit, then publish** (plan §2): a logged `UserRoleChanged` must be a committed fact. The use case
publishes right after `save`, so this root hands it `_PublishAfterCommit`, which only holds the
events; they reach `LoggingEventPublisher` after `session.commit()` returns, and never on a dry run,
a no-op or a failure (the rollback discards them with the transaction).

**The foreign-database guard** (`persistence/database_guard.py`, shared with `erase-account`) runs
before the use case reads anything.

**Nothing printed or logged names a person**: the user id (an opaque UUIDv7), role names, class
names. Never the email — this command never reads it into output — and never an exception's message
or `exc_info` (a driver error quotes the row it refused; CLAUDE.md, "A failed database write carries
its data out through three layers"). One run line, plus the published event on a real change.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.application.identity.change_user_role import ChangeUserRole
from tailorcraft.application.identity.results import RoleChange
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.value_objects import Role, UserId
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.events.logging_publisher import LoggingEventPublisher
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

EVENT_ROLE_CHANGE: Final = "identity.role_change"
EVENT_ROLE_CHANGE_FAILED: Final = "identity.role_change_failed"


class _PublishAfterCommit:
    """`EventPublisherPort` that holds what it is handed, so the caller can publish after commit."""

    def __init__(self) -> None:
        self.events: list[DomainEvent] = []

    async def publish(self, *events: DomainEvent) -> None:
        self.events.extend(events)


def run_from_cli(*, user_id: UUID, to: Role, dry_run: bool, command: str) -> int:
    """`tailorcraft.cli grant-role` / `revoke-role`. Returns an exit code; raises nothing an
    operator sees. Settings are read here and passed down; logs go to **stderr** so stdout carries
    the one-line report and nothing else. Sentry is not initialised: an operator at a terminal."""
    settings = get_settings()
    configure_logging(settings)
    _route_logs_to_stderr()
    return asyncio.run(
        change_role(settings, user_id=UserId(user_id), to=to, dry_run=dry_run, command=command)
    )


async def change_role(
    settings: Settings, *, user_id: UserId, to: Role, dry_run: bool, command: str
) -> int:
    """Move `user_id` to `to` against `settings.database_url` (or report what would happen, on a dry
    run), print one line, and return the exit code. The seam a test drives with a `Settings` it
    built — `get_settings()` under `APP_ENV=test` still names the **dev** database. `command` is
    `"grant-role"` or `"revoke-role"`, the stderr prefix.

    The engine is built inside the loop `asyncio.run` opened and disposed before it closes (an
    asyncpg connection is bound to its loop); `configure_mappings()` first. A commit happens only on
    a real change; a dry run or a no-op rolls back.
    """
    started_at = time.monotonic()
    configure_mappings()
    engine: AsyncEngine | None = None
    outbox = _PublishAfterCommit()
    try:
        # Inside the `try`: a malformed `DATABASE_URL` raises here, and is exit 1 like any other
        # database failure rather than a traceback.
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        async with factory() as session:
            try:
                await refuse_a_foreign_database(session, settings)
                # Deferred import: the repository reads mapped attributes at import, which exist only
                # once `configure_mappings()` has run (`deps.get_user_repository`'s reason).
                from tailorcraft.infrastructure.persistence.repositories.identity.user import (
                    SqlAlchemyUserRepository,
                )

                change = ChangeUserRole(SqlAlchemyUserRepository(session), SystemClock(), outbox)
                outcome = await change(user_id, to, dry_run=dry_run)
                if dry_run or not outcome.changed:
                    await session.rollback()
                else:
                    await session.commit()
            except Exception:
                await session.rollback()
                raise
    except ForeignDatabase as exc:
        _log_failure(exc, user_id, dry_run=dry_run, started_at=started_at)
        print(
            f"{command}: refused — connected to database {exc.actual!r}, but DATABASE_URL names "
            f"{exc.expected!r}. Nothing was read or changed.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    except UserNotFound as exc:
        _log_failure(exc, user_id, dry_run=dry_run, started_at=started_at)
        print(f"{command}: no account with id {user_id.value}.", file=sys.stderr)
        return EXIT_FAILED
    except Exception as exc:
        # The type, never the message and never `exc_info`: see the module docstring.
        _log_failure(exc, user_id, dry_run=dry_run, started_at=started_at)
        print(f"{command}: failed ({type(exc).__name__}).", file=sys.stderr)
        return EXIT_FAILED
    finally:
        if engine is not None:
            await engine.dispose()

    # Committed (or rolled back with nothing to say): only now is the event a fact worth logging.
    await LoggingEventPublisher().publish(*outbox.events)
    log.info(
        EVENT_ROLE_CHANGE,
        user_id=str(user_id.value),
        from_role=outcome.from_role.value,
        to_role=outcome.to_role.value,
        changed=outcome.changed,
        dry_run=dry_run,
        duration_ms=_elapsed_ms(started_at),
    )
    print(_report(outcome, dry_run=dry_run))
    return EXIT_OK


def _report(outcome: RoleChange, *, dry_run: bool) -> str:
    """AC-26's four stdout lines: ids and role names only."""
    user = outcome.user_id.value
    if outcome.changed:
        transition = f"role {outcome.from_role.value} → {outcome.to_role.value}"
        if dry_run:
            return f"would change user {user}: {transition} (dry run)"
        return f"user {user}: {transition}"
    held = f"user {user}: role already {outcome.to_role.value}"
    return f"{held}; nothing would change (dry run)" if dry_run else f"{held}; nothing changed"


def _log_failure(exc: Exception, user_id: UserId, *, dry_run: bool, started_at: float) -> None:
    log.warning(
        EVENT_ROLE_CHANGE_FAILED,
        error_type=type(exc).__name__,
        user_id=str(user_id.value),
        dry_run=dry_run,
        duration_ms=_elapsed_ms(started_at),
    )


def _route_logs_to_stderr() -> None:
    """stdout is the operator's report; `configure_logging` points at it. Same as `erase-account`."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(sys.stderr)


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)
