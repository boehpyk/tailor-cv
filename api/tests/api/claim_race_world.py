"""The committed-world helper shared by slice 2.4's HTTP concurrency proofs (T23): a
`concurrent_app` (a real session and connection per request) over a per-test upload volume, and the
bookkeeping to delete every user and guest session a test committed. See
`tests/integration/claim_race_support.py` for the connection and lock helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.api.deps import get_export_queue, get_tailoring_queue
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    Account,
    Entry,
    build_concurrent_app,
    mint_guest,
    new_client,
    override_settings,
    register,
    seed_entry,
)
from tests.integration.claim_race_support import assert_test_database, drop_rows
from tests.integration.fakes import FakeExportQueue, FakeTailoringQueue


class World:
    """One test's committed world. Call `cleanup()` in a `finally` (the fixtures do)."""

    def __init__(
        self, settings: Settings, engine: AsyncEngine, password_hasher: object, root: Path
    ) -> None:
        assert_test_database(settings)
        self.settings = settings.model_copy(update={"upload_dir": root})
        self.engine = engine
        self.root = root
        self.app: FastAPI = build_concurrent_app(self.settings, engine, password_hasher)
        override_settings(self.app, self.settings)
        self.app.dependency_overrides[get_tailoring_queue] = lambda: FakeTailoringQueue()
        self.app.dependency_overrides[get_export_queue] = lambda: FakeExportQueue()
        self.guests: list[UUID] = []
        self.users: list[UserId] = []

    async def account(self) -> Account:
        async with new_client(self.app) as setup:
            account = await register(setup, self.settings)
        self.users.append(account.user_id)
        return account

    async def mint(self, client: AsyncClient) -> GuestOwner:
        """A live guest on `client`: a real upload mints the session and the cookie."""
        async with async_sessionmaker(self.engine, expire_on_commit=False)() as session:
            guest = await mint_guest(client, session)
        self.guests.append(guest.guest_session_id.value)
        return guest

    async def token_hash(self, guest: GuestOwner) -> str:
        async with self.engine.connect() as conn:
            return str(
                (
                    await conn.execute(
                        text("SELECT token_hash FROM identity_guest_session WHERE id = :i"),
                        {"i": guest.guest_session_id.value},
                    )
                ).scalar_one()
            )

    async def guest_with_work(self, client: AsyncClient) -> tuple[GuestOwner, Entry, str]:
        """`mint` plus a seeded CV / posting / succeeded run / ready export (`seed_entry`)."""
        guest = await self.mint(client)
        async with async_sessionmaker(self.engine, expire_on_commit=False)() as session:
            entry = await seed_entry(
                session,
                self.settings,
                guest,
                at=datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10),
            )
        return guest, entry, await self.token_hash(guest)

    def files(self) -> set[str]:
        """Every file on the volume, as a path relative to its root (keys live in sub-folders)."""
        return {str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file()}

    async def cleanup(self) -> None:
        await drop_rows(self.engine, guests=self.guests, users=self.users)
