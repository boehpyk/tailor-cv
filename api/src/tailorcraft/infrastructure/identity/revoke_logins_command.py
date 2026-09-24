"""`tailorcraft.cli revoke-logins --all [--dry-run]` — the break-glass that signs everybody out
(AC-13, OQ-5), and its own composition root.

Rotating `JWT_SIGNING_KEY` does **not** log anybody out (I-44): refresh tokens are opaque database
rows, not signed values, so a new key costs every browser one silent refresh and nothing more. When
the intent really is "end every session now" — a leaked database dump, a compromised box — this is
the command, and deleting every `Login` is what it does.

**It is thin**, exactly like `purge-guests` (`infrastructure/retention/purge_command.py`), whose
exit-code table it shares:

| outcome | exit |
|---|---|
| success, including 0 logins | **0** |
| the database failed (connect, count, delete or commit) | **1**, the exception's **type** only |
| no `--all`, or any other usage error | **2** — argparse's own, raised in `cli.py` before this runs |

There is no lock and so no exit 3: the delete is one statement, and two concurrent runs are two
idempotent statements — the second deletes what the first left, which is nothing.

**`--all` is required and is the whole safety of the command.** The use case has no filter and this
command has no other mode; the flag exists so that `revoke-logins` typed by itself — or tab-completed
by someone looking for help — is a usage error rather than a mass sign-out.

**Nothing printed or logged names a person.** The use case returns an `int`; the failure line
carries `error_type` and never the exception's message or `exc_info`, because a driver error quotes
the row it refused (CLAUDE.md, "A failed database write carries its data out through three layers").
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Final

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.infrastructure.identity.composition import build_revoke_all_logins
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.persistence.database import create_engine, create_session_factory
from tailorcraft.infrastructure.persistence.registry import configure_mappings
from tailorcraft.infrastructure.settings import Settings, get_settings

log = structlog.get_logger(__name__)

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1

EVENT_LOGINS_REVOKED: Final = "identity.logins_revoked"
EVENT_LOGINS_REVOKE_FAILED: Final = "identity.logins_revoke_failed"


def run_from_cli(*, dry_run: bool) -> int:
    """`tailorcraft.cli revoke-logins --all`. Returns an exit code; raises nothing an operator sees.

    Settings are read here and passed down (`os.environ` is read in exactly one place), and logs go
    to **stderr** so stdout carries the one-line report and nothing else — `purge-guests`'s reason.
    Sentry is not initialised: an operator at a terminal, not a service.
    """
    settings = get_settings()
    configure_logging(settings)
    _route_logs_to_stderr()
    return asyncio.run(revoke_logins(settings, dry_run=dry_run))


async def revoke_logins(settings: Settings, *, dry_run: bool) -> int:
    """Run `RevokeAllLogins(dry_run)` against `settings.database_url`, print the count, and return
    the exit code. The seam a test drives with a `Settings` it built — `get_settings()` under
    `APP_ENV=test` still names the **dev** database, and this command deletes.

    **The engine is built here, inside the loop `asyncio.run` opened, and disposed before it
    closes** (an asyncpg connection is bound to the loop that created it). `configure_mappings()`
    first: the repository reads mapped attributes at import.

    **A dry run commits nothing** — it rolls back its read transaction — so "deletes nothing" is a
    property of this branch rather than of the use case alone.
    """
    started_at = time.monotonic()
    configure_mappings()
    engine: AsyncEngine | None = None
    try:
        # Inside the `try`: a malformed `DATABASE_URL` raises here, and is exit 1 like any other
        # database failure rather than a traceback.
        engine = create_engine(settings)
        factory = create_session_factory(engine)
        async with factory() as session:
            try:
                count = await build_revoke_all_logins(session)(dry_run)
                if dry_run:
                    await session.rollback()
                else:
                    await session.commit()
            except Exception:
                await session.rollback()
                raise
    except Exception as exc:
        # The type, never the message and never `exc_info`: see the module docstring.
        log.warning(
            EVENT_LOGINS_REVOKE_FAILED,
            error_type=type(exc).__name__,
            dry_run=dry_run,
            duration_ms=_elapsed_ms(started_at),
        )
        print(f"revoke-logins: failed ({type(exc).__name__}).", file=sys.stderr)
        return EXIT_FAILED
    finally:
        if engine is not None:
            await engine.dispose()

    log.info(
        EVENT_LOGINS_REVOKED,
        count=count,
        dry_run=dry_run,
        duration_ms=_elapsed_ms(started_at),
    )
    noun = "login" if count == 1 else "logins"
    if dry_run:
        print(f"would revoke {count} {noun} (dry run)")
    else:
        print(f"revoked {count} {noun}")
    return EXIT_OK


def _route_logs_to_stderr() -> None:
    """stdout is the operator's report; `configure_logging` points at it. Same as `purge-guests`."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(sys.stderr)


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)
