"""Alembic environment.

Two things here are specific to this codebase and worth reading before changing:

1. **The metadata comes from the imperative mapping registry**, and `configure_mappings()` must run
   before `target_metadata` is used. Declarative mapping populates metadata as a side effect of
   importing the model classes; imperative mapping does not — a mapping module nobody imports
   contributes no table, and autogenerate would then cheerfully propose dropping it.
2. **The URL comes from settings**, not from alembic.ini, so there is exactly one place that reads
   the environment.

And the standing rule from ADR-0007: **autogenerate output is a draft.** Read every line.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from tailorcraft.infrastructure.persistence.registry import configure_mappings, metadata
from tailorcraft.infrastructure.settings import get_settings

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False` is load-bearing, not a preference. The default is True, and
    # it sets `.disabled = True` on every logger that already exists — 24 of them here, including
    # `pypdf`, `docx`, `celery`, `redis`, `sqlalchemy`, `sentry_sdk` and `httpx`, none of which
    # `alembic.ini` so much as mentions. `.disabled` short-circuits `isEnabledFor` before the level
    # is ever consulted, so it silences more thoroughly than any level we set in
    # `infrastructure/observability.py`.
    #
    # That matters because `tests/conftest.py::_migrated` is SESSION-scoped: one migration run would
    # otherwise switch those loggers off for the whole suite, and a database rollback does not turn
    # them back on. Any test of the form "X never appears in the logs" — AC-12's privacy tests
    # above all — could then pass because nothing was logging at all. A gate that checks a different
    # thing than it claims is worse than no gate, because it also supplies confidence.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

configure_mappings()
target_metadata = metadata

# Only fall back to settings when the caller has not already picked a URL. `alembic.ini` never sets
# `sqlalchemy.url` (see its header comment), so the plain CLI (`make migrate`, `make
# migration.make`) always hits this branch and gets `settings.database_url` — the dev database,
# correctly. But `tests/conftest.py::_migrated` calls `config.set_main_option("sqlalchemy.url",
# settings.test_database_url)` *before* invoking Alembic, specifically so the suite migrates
# `tailorcraft_test` and never the dev database (CLAUDE.md). Overwriting unconditionally here — as
# an earlier version of this file did — silently discarded that override: `get_settings()` is
# `lru_cache`d, so by the time `env.py` ran it returned the *first* `Settings()` built in the
# process (the one the `settings` pytest fixture read before applying its `model_copy` override),
# never the test URL. The result was a test suite that quietly migrated the dev database. Caught
# only once this migration actually created tables to look for.
if not config.get_main_option("sqlalchemy.url"):
    settings = get_settings()
    config.set_main_option("sqlalchemy.url", settings.database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Without this, a column whose type changed is silently ignored by autogenerate.
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
