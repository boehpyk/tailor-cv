"""Shared seeds for slice 4.4's two admin API test files (`test_admin_guests.py`,
`test_admin_retention.py`).

A **concurrent** app (a real, committed session per request): every claim here is about what another
connection sees. Uploads go to a **per-test** directory (never the shared `upload_dir`: the storage
overview and the orphan sweep walk their whole root, 2.2's rule), and every cross-request fact is
read on `engine.connect()`. Sessions and accounts are real and deleted by id in teardown.

Guest sessions are written through the real repository and `GuestSession.start`, with a token minted
by the app's own `mint_guest_token`, so a seeded session is one the domain could have produced and
its cookie is one the guest's own routes accept. Everything **commits** (2.3's `fa40793`).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.api.guest_session import COOKIE_NAME, mint_guest_token
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    Entry,
    assert_test_database,
    bearer,
    build_concurrent_app,
    new_client,
    override_settings,
    seed_entry,
    seed_user_and_sign_in,
)
from tests.integration.owners import extracted_cv

UNMATCHED = "/api/admin/no-such-route"
GUESTS = "/api/admin/guest-sessions"

# Tables a guest session owns rows in (the cascade takes them with the session).
GUEST_CHILD_TABLES = ("intake_base_cv", "posting_job_posting", "tailoring_run", "export_job")


@dataclass
class Who:
    """An account with a client of its own (so its `tc_refresh` cookie is not another's)."""

    client: AsyncClient
    token: str
    id: UUID

    @property
    def headers(self) -> dict[str, str]:
        return bearer(self.token)

    @property
    def owner(self) -> UserOwner:
        return UserOwner(UserId(self.id))


@dataclass
class Guest:
    """A seeded guest session: its id, its raw cookie token and its hash (never to be returned)."""

    id: GuestSessionId
    token: str
    token_hash: str
    created_at: datetime
    expires_at: datetime

    @property
    def owner(self) -> GuestOwner:
        return GuestOwner(self.id)

    @property
    def uuid(self) -> UUID:
        return self.id.value


@dataclass
class World:
    app: FastAPI
    settings: Settings
    engine: AsyncEngine
    upload_dir: Path
    hasher: Argon2PasswordHasher
    http: AsyncClient
    _clients: list[AsyncClient] = field(default_factory=list)
    _users: list[UUID] = field(default_factory=list)
    _guests: list[UUID] = field(default_factory=list)

    # ------------------------------------------------------------------ accounts

    async def account(self, *, admin: bool = False) -> Who:
        client = new_client(self.app)
        self._clients.append(client)
        token, user_id = await seed_user_and_sign_in(client, self.settings)
        self._users.append(user_id)
        if admin:
            async with self.engine.connect() as conn:
                await conn.execute(
                    text("UPDATE identity_user SET role = 'admin' WHERE id = :i"), {"i": user_id}
                )
                await conn.commit()
        return Who(client, token, user_id)

    # ------------------------------------------------------------------ guests

    async def guest(
        self,
        *,
        started: timedelta = timedelta(minutes=1),
        ttl_hours: int = 24,
        at: datetime | None = None,
    ) -> Guest:
        """A guest session started `started` ago (or at `at`) and living `ttl_hours`: live by
        default; pass `started=timedelta(hours=30)` for one that expired six hours ago."""
        now = SystemClock().now()
        minted = mint_guest_token()
        async with self.app.state.session_factory() as session:
            repo = SqlAlchemyGuestSessionRepository(session)
            started_at = at or (now - started)
            row = GuestSession.start(repo.next_identity(), minted.token_hash, started_at, ttl_hours)
            await repo.add(row)
            await session.commit()
        self._guests.append(row.id.value)
        return Guest(row.id, minted.token, minted.token_hash, row.created_at, row.expires_at)

    def guest_client(self, guest: Guest) -> AsyncClient:
        """A client carrying exactly this session's `__Host-tc_guest` — the guest's own seat."""
        client = new_client(self.app)
        client.cookies.set(COOKIE_NAME, guest.token)
        self._clients.append(client)
        return client

    # ------------------------------------------------------------------ content

    async def entry(
        self, owner: GuestOwner | UserOwner, *, minutes_ago: int = 10, **kwargs: Any
    ) -> Entry:
        """CV row, posting, succeeded run and `ready` exports (their files on disk); the CV's own
        file is written too so a session's file set is real."""
        at = SystemClock().now() - timedelta(minutes=minutes_ago)
        async with self.app.state.session_factory() as session:
            entry = await seed_entry(session, self.settings, owner, at=at, **kwargs)
        await LocalFileStore(self.upload_dir).put(FileRef.for_base_cv(entry.cv_id, _PDF), CV_BYTES)
        return entry

    async def cv(self, owner: GuestOwner | UserOwner, *, minutes_ago: int = 10) -> BaseCv:
        """An extracted CV row with its file on disk."""
        at = SystemClock().now() - timedelta(minutes=minutes_ago)
        cv = extracted_cv(owner, at)
        async with self.app.state.session_factory() as session:
            await SqlAlchemyBaseCvRepository(session).add(cv)
            await session.commit()
        await LocalFileStore(self.upload_dir).put(cv.file, CV_BYTES)
        return cv

    async def orphan(
        self, *, age: timedelta = timedelta(days=5), body: bytes = b"orphan"
    ) -> FileRef:
        """A well-formed key no row names, written to the volume and aged by `mtime`."""
        ref = FileRef.for_base_cv(BaseCvId(uuid4()), _PDF)
        await LocalFileStore(self.upload_dir).put(ref, body)
        self.age(ref.key, age)
        return ref

    def age(self, relative: str, age: timedelta) -> None:
        path = self.upload_dir / relative
        stamp = (SystemClock().now() - age).timestamp()
        os.utime(path, (stamp, stamp))

    def path(self, ref: FileRef) -> Path:
        return self.upload_dir / ref.key

    # ------------------------------------------------------------------ facts (fresh connection)

    async def count(self, sql: str, **params: object) -> int:
        async with self.engine.connect() as conn:
            return int((await conn.execute(text(sql), params)).scalar_one())

    async def rows_of_guest(self, guest: Guest) -> dict[str, int]:
        counts = {
            "identity_guest_session": await self.count(
                "SELECT count(*) FROM identity_guest_session WHERE id = :i", i=guest.uuid
            )
        }
        for table in GUEST_CHILD_TABLES:
            counts[table] = await self.count(
                f"SELECT count(*) FROM {table} WHERE guest_session_id = :i",  # noqa: S608
                i=guest.uuid,
            )
        return counts

    async def rows_of_user(self, user: Who) -> dict[str, int]:
        return {
            table: await self.count(
                f"SELECT count(*) FROM {table} WHERE user_id = :i",  # noqa: S608
                i=user.id,
            )
            for table in GUEST_CHILD_TABLES
        }

    async def expire(self, guest: Guest) -> None:
        """Make a live session expired, on a connection of its own (it expired mid-browse)."""
        async with self.engine.connect() as conn:
            await conn.execute(
                text("UPDATE identity_guest_session SET expires_at = :t WHERE id = :i"),
                {"t": SystemClock().now() - timedelta(hours=1), "i": guest.uuid},
            )
            await conn.commit()

    async def delete_all_guest_sessions(self) -> None:
        """A clean slate for tests about absolute counts. Guarded: the **test** database only."""
        assert_test_database(self.settings)
        async with self.engine.begin() as conn:
            await conn.execute(text("DELETE FROM identity_guest_session"))


_PDF = CvContentType.PDF
CV_BYTES = b"original upload bytes of a guest cv"


def _prepare(upload_dir: Path) -> None:
    upload_dir.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def open_world(
    settings: Settings,
    engine: AsyncEngine,
    hasher: Argon2PasswordHasher,
    upload_dir: Path,
    *,
    clean_guest_sessions: bool = False,
) -> AsyncIterator[World]:
    """The app, an anonymous client, a per-test upload directory; everything seeded is deleted."""
    assert_test_database(settings)
    _prepare(upload_dir)
    app = build_concurrent_app(settings, engine, hasher)
    own = override_settings(app, settings, upload_dir=upload_dir)
    async with new_client(app) as http:
        world = World(app, own, engine, upload_dir, hasher, http)
        if clean_guest_sessions:
            await world.delete_all_guest_sessions()
        try:
            yield world
        finally:
            for client in world._clients:
                await client.aclose()
            async with engine.begin() as conn:
                for user_id in world._users:
                    await conn.execute(
                        text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id}
                    )
                for guest_id in world._guests:
                    await conn.execute(
                        text("DELETE FROM identity_guest_session WHERE id = :i"), {"i": guest_id}
                    )
