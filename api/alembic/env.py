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
    fileConfig(config.config_file_name)

configure_mappings()
target_metadata = metadata

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
