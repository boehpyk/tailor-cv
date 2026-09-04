"""Engine and session lifecycle.

The unit of work opens at a boundary — an HTTP request or a Celery task — and commits there. No
`AsyncSession` is ever passed into the application layer: a use case receives repository *ports*,
which is what lets it be tested without a database and re-pointed at a different store without an
edit (ADR-0002).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tailorcraft.infrastructure.settings import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine.

    `pool_pre_ping` costs one cheap round trip per checkout and buys immunity to the single most
    annoying production symptom there is: a connection the pool believes is alive because nothing
    told it that PostgreSQL restarted, surfacing as one failed request after every deploy.
    """
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        echo=False,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory.

    `expire_on_commit=False` because objects are read after the commit that saved them — with the
    default, every attribute access after a commit triggers a lazy refresh, which in an async
    session raises rather than quietly issuing SQL. Loud, but only once you hit it.
    """
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One transaction, committed on success and rolled back on any exception."""
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
