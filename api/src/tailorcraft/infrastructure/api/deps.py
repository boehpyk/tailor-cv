"""FastAPI dependency wiring — the composition root.

This is the one place that knows which adapter satisfies which port. A route declares what it needs;
this module decides what it gets. Keeping that decision in a single file is what makes "is every
port bound?" a question with one place to look, rather than an archaeology exercise across the
codebase.

Long-lived resources — the engine, the session factory — are created once at startup and stashed on
`app.state`, not rebuilt per request. An engine constructed per request means a connection pool
constructed per request, which is a pool of one that never gets reused.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tailorcraft.domain.shared.clock import Clock
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.settings import Settings, get_settings


def get_app_settings() -> Settings:
    return get_settings()


def get_engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, committed on success and rolled back on any exception.

    The unit of work is a property of the *boundary*, not of the use case — which is why this lives
    here and why no `AsyncSession` is ever passed into the application layer.
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


def get_clock() -> Clock:
    """The only source of "now" the application layer may use."""
    return SystemClock()


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
EngineDep = Annotated[AsyncEngine, Depends(get_engine)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ClockDep = Annotated[Clock, Depends(get_clock)]
