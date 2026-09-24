"""Database truths for `identity-register-and-login` (T31, test-after): the half of the failure
contract that can only be proved against the **real** app over the **real** database, because it is
about what two independent connections do to one another — a rolled-back, single-`AsyncSession`
request cannot race itself.

**AC-16 / AC-17 need genuine concurrency, and `tests/conftest.py`'s `app` fixture cannot give it.**
That fixture wires every request in a test to `get_session` overridden to the **same** already-open
`AsyncSession` (`_committing_session_override`) — exactly right for isolation (SAVEPOINT rollback,
one test cannot see another's data) and exactly wrong here, because a single `AsyncSession` is not
safe for concurrent use: two coroutines racing statements through it would not reproduce two
independent HTTP clients contending for one database row, they would corrupt one session object.
This file builds its own `app` fixture instead, wired the way `main.py`'s lifespan wires production —
`app.state.session_factory = create_session_factory(engine)`, so **every request opens and commits
its own session** off the shared connection pool — and every write in this file is therefore a real,
committed row, cleaned up by hand at teardown, exactly as `test_purge_cli.py` and
`test_purge_database.py` already do for the same reason (their own module docstrings have the full
account of why the ordinary `session`/`connection` fixtures cannot be used for a test that needs a
second, independent, genuinely committing connection).

**The one rule that dominates every deleting test in this codebase, CLAUDE.md's 1.4 incident.**
`get_settings()` under `APP_ENV=test` still returns the **dev** `database_url` — only the `settings`
fixture swaps in `test_database_url`. `_assert_test_database` asserts `"_test"` is in the URL about
to be used, before the first statement, in every test below that writes.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import RowMapping, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    GuestSessionId,
    LoginId,
    PasswordHash,
    TokenHash,
    UserId,
)
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import CvContentType, OriginalFilename
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.api.guest_session import COOKIE_NAME as GUEST_COOKIE_NAME
from tailorcraft.infrastructure.api.guest_session import mint_guest_token
from tailorcraft.infrastructure.api.main import create_app
from tailorcraft.infrastructure.api.refresh_cookie import (
    COOKIE_NAME as REFRESH_COOKIE_NAME,
)
from tailorcraft.infrastructure.api.refresh_cookie import hash_refresh_token, mint_refresh_token
from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.persistence.database import create_session_factory
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.identity.login import (
    login_table,
    retired_refresh_token_table,
)
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.retention import purge_command
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks.app import app as celery_app

REGISTER_URL = "/api/auth/register"
REFRESH_URL = "/api/auth/refresh"
BASE_CVS_URL = "/api/base-cvs"

A_STRONG_PASSWORD = "correct horse battery staple 9"  # 31 chars, clear of the 12-char floor.


def _assert_test_database(settings: Settings) -> None:
    """CLAUDE.md's 1.4 guard, reproduced locally like every other deleting test file in this suite —
    kept local on purpose (`test_purge_database.py`'s own docstring explains why promoting it is
    declined)."""
    assert "_test" in settings.database_url, (
        "refusing to run a deleting identity test against a URL that is not the test database: "
        f"{settings.database_url!r}"
    )


def _origin_headers(settings: Settings) -> dict[str, str]:
    return {"Origin": settings.public_base_url}


def _a_past_instant(hours_ago: float) -> datetime:
    """A real, whole-second, timezone-aware wall-clock instant. This file's app is wired with the
    production `get_clock` (never overridden — see the module docstring: every request opens its own
    session, so there is no single test-owned `FixedClock` to hand to a dependency override that would
    mean anything across two independent clients), so "expired" must be real past time."""
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).replace(microsecond=0)


# --- The real, concurrency-capable app --------------------------------------------------------


@pytest.fixture
def concurrent_app(
    settings: Settings, engine: AsyncEngine, password_hasher: Argon2PasswordHasher
) -> FastAPI:
    """An application wired for genuine per-request sessions (module docstring). Every write a test
    drives through this app's clients is a real, committed row against `tailorcraft_test` — nothing
    here is rolled back automatically, and every test that writes cleans up in a `finally`."""
    app = create_app(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.celery = celery_app
    app.state.password_hasher = password_hasher
    return app


def _new_client(app: FastAPI) -> AsyncClient:
    """A fresh client with its own cookie jar, against the same app — the shape a second browser
    tab or a second concurrent registration attempt actually has."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _rotated_refresh_token(response: Response) -> str:
    """The new `tc_refresh` value straight off `Set-Cookie`, never off `client.cookies` — the jar
    would hold this value *and* whatever was set by hand before the request (a different
    domain/path pair from the same name), and `Cookies.get` refuses to guess between them
    (`httpx.CookieConflict`)."""
    headers = response.headers.get_list("set-cookie")
    for header in headers:
        if header.startswith(f"{REFRESH_COOKIE_NAME}="):
            value = header.split(";", 1)[0].partition("=")[2]
            return value
    raise AssertionError(f"no Set-Cookie: {REFRESH_COOKIE_NAME}=... in {headers!r}")


# --- A committed rig: real users, logins and retired hashes, deleted at teardown ---------------


@dataclass(slots=True)
class _CommittedIdentity:
    engine: AsyncEngine
    _user_ids: list[UserId] = field(default_factory=list)

    def _session(self) -> AsyncSession:
        factory = async_sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)
        return factory()

    async def seed_login(
        self,
        *,
        email: str | None = None,
        started_at: datetime | None = None,
        lifetime: timedelta = timedelta(days=30),
    ) -> tuple[UserId, LoginId, str]:
        """A real, committed `User` with one `Login` at generation 1. Returns `(user_id, login_id,
        raw_token)` — the raw token is the value a test sets as the `tc_refresh` cookie."""
        now = started_at or datetime.now(UTC).replace(microsecond=0)
        async with self._session() as session:
            user_id = UserId(uuid4())
            user = User.register_with_password(
                user_id,
                EmailAddress.parse(email or f"t31-{uuid4().hex}@example.com"),
                PasswordHash("$argon2id$v=19$m=8,t=1,p=1$c2FsdA$dummy"),
                now,
            )
            session.add(user)
            await session.flush()

            minted = mint_refresh_token()
            login_id = LoginId(uuid4())
            login = Login.start(login_id, user_id, minted.token_hash, now, lifetime)
            session.add(login)
            await session.commit()
        self._user_ids.append(user_id)
        return user_id, login_id, minted.token

    async def user_count(self) -> int:
        async with self.engine.connect() as conn:
            result = await conn.execute(select(func.count()).select_from(user_table))
            return result.scalar_one()

    async def user_count_for_email(self, email: str) -> int:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(user_table)
                .where(user_table.c.email == EmailAddress.parse(email))
            )
            return result.scalar_one()

    async def login_row(self, login_id: LoginId) -> RowMapping | None:
        async with self.engine.connect() as conn:
            result = await conn.execute(select(login_table).where(login_table.c.id == login_id))
            return result.mappings().one_or_none()

    async def retired_count(self, login_id: LoginId) -> int:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(retired_refresh_token_table)
                .where(retired_refresh_token_table.c.login_id == login_id)
            )
            return result.scalar_one()

    async def cleanup(self) -> None:
        if not self._user_ids:
            return
        async with self.engine.begin() as conn:
            await conn.execute(user_table.delete().where(user_table.c.id.in_(self._user_ids)))


@pytest_asyncio.fixture
async def identity_rig(engine: AsyncEngine) -> AsyncIterator[_CommittedIdentity]:
    rig = _CommittedIdentity(engine=engine)
    try:
        yield rig
    finally:
        await rig.cleanup()


# --- AC-16: two concurrent registrations of one email -------------------------------------------


async def test_two_concurrent_registrations_of_one_email_produce_one_201_one_409_one_row(
    concurrent_app: FastAPI, settings: Settings
) -> None:
    """AC-16 / I-6. Two independent clients, two independent sessions, one `Origin`-trusted `POST
    /api/auth/register` each, launched together with `asyncio.gather` so both are genuinely in
    flight before either's `INSERT` lands — the unique index on `identity_user.email`
    (`uq_identity_user_email`) is the referee, never a `SELECT` first (technical plan §0.4)."""
    _assert_test_database(settings)
    email = f"t31-ac16-{uuid4().hex}@example.com"
    body = {"email": email, "password": A_STRONG_PASSWORD}
    headers = _origin_headers(settings)

    async def _register() -> int:
        async with _new_client(concurrent_app) as client:
            response = await client.post(REGISTER_URL, json=body, headers=headers)
            return response.status_code

    try:
        codes = await asyncio.gather(_register(), _register())

        assert sorted(codes) == [201, 409], codes
        rig = _CommittedIdentity(engine=concurrent_app.state.engine)
        assert await rig.user_count_for_email(email) == 1
    finally:
        async with concurrent_app.state.engine.begin() as conn:
            await conn.execute(
                user_table.delete().where(user_table.c.email == EmailAddress.parse(email))
            )


# --- AC-17 / I-25: two concurrent refreshes of one current token --------------------------------


async def test_two_concurrent_refreshes_of_one_current_token_produce_one_200_one_409(
    concurrent_app: FastAPI, settings: Settings, identity_rig: _CommittedIdentity
) -> None:
    """AC-17 / I-25. One real, committed `Login` at generation 1; two independent clients present its
    **current** token to `POST /api/auth/refresh` at once. `SqlAlchemyLoginRepository.save_rotation`'s
    `UPDATE … WHERE version = :loaded` is what decides this under READ COMMITTED (that module's own
    docstring): the loser's `UPDATE` waits on the winner's row lock, re-evaluates its `WHERE` against
    the now-committed row, finds no match, and `RefreshLogin` answers 409 `refresh_in_progress` —
    never a revocation, and the login survives with its generation advanced exactly once."""
    _assert_test_database(settings)
    _user_id, login_id, raw_token = await identity_rig.seed_login()
    headers = _origin_headers(settings)

    async def _refresh() -> int:
        async with _new_client(concurrent_app) as client:
            client.cookies.set(REFRESH_COOKIE_NAME, raw_token)
            response = await client.post(REFRESH_URL, headers=headers)
            return response.status_code

    codes = await asyncio.gather(_refresh(), _refresh())

    assert sorted(codes) == [200, 409], codes
    row = await identity_rig.login_row(login_id)
    assert row is not None, "the login must still exist — never a revocation on a race (AC-17)"
    assert row["generation"] == 2, "generation must advance exactly once, not twice"


# --- I-24's commit: after a reuse-401, a FRESH session finds nothing at all ----------------------


async def test_after_a_reuse_401_a_fresh_session_finds_no_login_and_no_retired_rows(
    concurrent_app: FastAPI, settings: Settings, identity_rig: _CommittedIdentity
) -> None:
    """I-24, the commit half. Two real rotations put the login at generation 3 with its very first
    token (generation 1) retired; presenting that first token again is `judge_retired`'s **REUSED**
    case immediately (`current - 1 == 2 != 1`, no need to wait out the 10 s grace — REUSED at 0 s is
    the spec's own example). The use case deletes the login and publishes before raising, and
    `routers/auth.py` commits that deletion **inside** the request before returning 401 (technical
    plan §2). This test opens a brand-new connection afterwards — never the one that ran the
    request — and finds the login row and every one of its retired hashes gone."""
    _assert_test_database(settings)
    _user_id, login_id, first_token = await identity_rig.seed_login()
    headers = _origin_headers(settings)

    async with _new_client(concurrent_app) as client:
        client.cookies.set(REFRESH_COOKIE_NAME, first_token)
        first = await client.post(REFRESH_URL, headers=headers)
        assert first.status_code == 200, first.text
        second_token = _rotated_refresh_token(first)
        assert second_token != first_token

        # A clean jar for each step: the cookie set by hand above and the one the server just
        # returned would otherwise coexist under different (domain, path) pairs and make the next
        # `.get()` ambiguous (this is what `_rotated_refresh_token` reads around, but a stale entry
        # left in the jar would still be sent on the *next* request otherwise).
        client.cookies.clear()
        client.cookies.set(REFRESH_COOKIE_NAME, second_token)
        second = await client.post(REFRESH_URL, headers=headers)
        assert second.status_code == 200, second.text

        # `first_token`'s hash is retired at generation 1; current generation is now 3.
        client.cookies.clear()
        client.cookies.set(REFRESH_COOKIE_NAME, first_token)
        reused = await client.post(REFRESH_URL, headers=headers)

    assert reused.status_code == 401, reused.text
    assert reused.json()["error"]["code"] == "refresh_token_reused"

    # A fresh session, opened after the request above has already returned — proves the deletion is
    # committed, not merely visible inside the request's own (already-closed) transaction.
    row = await identity_rig.login_row(login_id)
    assert row is None, "the login row must be gone after a committed reuse-401"
    assert await identity_rig.retired_count(login_id) == 0, (
        "every retired hash must have gone with it (ON DELETE CASCADE)"
    )


async def test_after_an_expired_login_401_a_fresh_session_finds_no_login_row(
    concurrent_app: FastAPI, settings: Settings, identity_rig: _CommittedIdentity
) -> None:
    """I-21's own commit half, the sibling case named alongside I-24's in T31. A login seeded with a
    `created_at` an hour in the past and a one-second lifetime is expired by real wall-clock time the
    moment this test runs; presenting its current token raises `LoginExpired` inside `Login.rotate`,
    which the use case answers by deleting the login on sight and raising `LoginNotFound(EXPIRED)` —
    `routers/auth.py` commits that deletion inside the request (module docstring, same mechanism as
    I-24). A fresh connection afterward finds no row."""
    _assert_test_database(settings)
    _user_id, login_id, raw_token = await identity_rig.seed_login(
        started_at=_a_past_instant(1), lifetime=timedelta(seconds=1)
    )
    headers = _origin_headers(settings)

    async with _new_client(concurrent_app) as client:
        client.cookies.set(REFRESH_COOKIE_NAME, raw_token)
        response = await client.post(REFRESH_URL, headers=headers)

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "not_signed_in"
    row = await identity_rig.login_row(login_id)
    assert row is None, "an expired login must be deleted on sight, and that deletion committed"


# --- AC-31: a full purge over a registered user beside an expired guest session ------------------


async def test_a_full_purge_deletes_zero_rows_from_the_three_identity_tables(
    settings: Settings, engine: AsyncEngine, identity_rig: _CommittedIdentity, clear_redis: None
) -> None:
    """AC-31, proved the way R-42 requires: direct table counts, never the `PurgeReport`. One
    registered user with **two** logins and, between them, **five** retired hashes sits beside one
    genuinely expired guest session; a real `purge-guests` run (the exact composition root
    `run_from_cli` and 1.6's own tests use, `purge_command._purge_guests`) must not remove a single
    row from `identity_user`, `identity_login` or `identity_retired_refresh_token` — there is no
    foreign key from any of the three to `identity_guest_session` for it to reach through (AC-15),
    and the purge's own predicate never names these tables at all.
    """
    _assert_test_database(settings)

    # One user, two logins (a second device).
    user_id, login_a, token_a = await identity_rig.seed_login()
    async with identity_rig._session() as session:
        login_b_id = LoginId(uuid4())
        minted_b = mint_refresh_token()
        login_b = Login.start(
            login_b_id,
            user_id,
            minted_b.token_hash,
            datetime.now(UTC).replace(microsecond=0),
            timedelta(days=30),
        )
        session.add(login_b)
        await session.commit()

    # Rotate login_a three times and login_b twice — five retired hashes total — through the real
    # repository, exactly as `RefreshLogin` would.
    from tailorcraft.infrastructure.persistence.repositories.identity.login import (
        SqlAlchemyLoginRepository,
    )

    async def _rotate(current_hash: TokenHash, times: int) -> None:
        for _ in range(times):
            async with identity_rig._session() as session:
                repo = SqlAlchemyLoginRepository(session)
                login = await repo.find_by_current_token_hash(current_hash)
                assert login is not None
                new_hash = mint_refresh_token().token_hash
                retired = login.rotate(new_hash, datetime.now(UTC).replace(microsecond=0))
                await repo.save_rotation(login, retired)
                await session.commit()
                current_hash = new_hash

    await _rotate(hash_refresh_token(token_a), 3)
    await _rotate(minted_b.token_hash, 2)

    assert await identity_rig.retired_count(login_a) == 3
    assert await identity_rig.retired_count(login_b_id) == 2

    # An expired, unrelated guest session beside it, exactly the shape 1.6's own tests use.
    guest_session_id = GuestSessionId(uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            guest_session_table.insert().values(
                id=guest_session_id,
                token_hash=secrets.token_hex(32),
                created_at=_a_past_instant(25),
                expires_at=_a_past_instant(1),
            )
        )

    before_user = await identity_rig.user_count()
    async with engine.connect() as conn:
        before_login = (
            await conn.execute(select(func.count()).select_from(login_table))
        ).scalar_one()
        before_retired = (
            await conn.execute(select(func.count()).select_from(retired_refresh_token_table))
        ).scalar_one()

    try:
        exit_code = await purge_command._purge_guests(settings, dry_run=False, limit=None)
        assert exit_code == purge_command.EXIT_OK

        async with engine.connect() as conn:
            after_user = (
                await conn.execute(select(func.count()).select_from(user_table))
            ).scalar_one()
            after_login = (
                await conn.execute(select(func.count()).select_from(login_table))
            ).scalar_one()
            after_retired = (
                await conn.execute(select(func.count()).select_from(retired_refresh_token_table))
            ).scalar_one()
            guest_gone = (
                await conn.execute(
                    select(func.count())
                    .select_from(guest_session_table)
                    .where(guest_session_table.c.id == guest_session_id)
                )
            ).scalar_one()

        assert after_user == before_user, "the purge deleted a registered user"
        assert after_login == before_login, "the purge deleted a login"
        assert after_retired == before_retired, "the purge deleted a retired refresh token"
        assert guest_gone == 0, "the expired guest session should have been purged (test setup)"
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                guest_session_table.delete().where(guest_session_table.c.id == guest_session_id)
            )


# --- AC-29's missing clause: registering with a live tc_guest leaves it and its CVs untouched ----


async def test_registering_with_a_live_guest_cookie_leaves_the_session_and_its_cvs_readable(
    concurrent_app: FastAPI, settings: Settings, engine: AsyncEngine
) -> None:
    """AC-29. A registration carrying a live `tc_guest` must not read, set, rotate or clear that
    cookie: no `Set-Cookie: tc_guest` on the response, the `identity_guest_session` row byte-identical
    afterwards (including `expires_at` — the retention promise frozen at `GuestSession.start`, 1.6's
    R-16), and its base CV still readable with the same cookie once registration is done.
    """
    from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
        SqlAlchemyBaseCvRepository,
    )

    _assert_test_database(settings)
    guest_session_id = GuestSessionId(uuid4())
    minted = mint_guest_token()
    now = datetime.now(UTC).replace(microsecond=0)
    async with engine.begin() as conn:
        await conn.execute(
            guest_session_table.insert().values(
                id=guest_session_id,
                token_hash=minted.token_hash,
                created_at=now,
                expires_at=now + timedelta(hours=24),
            )
        )

    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        cvs = SqlAlchemyBaseCvRepository(session)
        cv_id = cvs.next_identity()
        ref = FileRef.for_base_cv(cv_id, CvContentType.PDF)

        await cvs.add(
            BaseCv.upload(
                id=cv_id,
                guest_session_id=guest_session_id,
                original_filename=OriginalFilename("cv.pdf"),
                content_type=CvContentType.PDF,
                size_bytes=8,
                file=ref,
                uploaded_at=now,
            )
        )
        await session.commit()

    async def _snapshot() -> RowMapping:
        async with engine.connect() as conn:
            result = await conn.execute(
                select(guest_session_table).where(guest_session_table.c.id == guest_session_id)
            )
            return result.mappings().one()

    before = dict(await _snapshot())

    email = f"t31-ac29-{uuid4().hex}@example.com"
    try:
        async with _new_client(concurrent_app) as client:
            client.cookies.set(GUEST_COOKIE_NAME, minted.token)
            response = await client.post(
                REGISTER_URL,
                json={"email": email, "password": A_STRONG_PASSWORD},
                headers=_origin_headers(settings),
            )
            assert response.status_code == 201, response.text
            set_cookie_names = {h.split("=", 1)[0] for h in response.headers.get_list("set-cookie")}
            assert GUEST_COOKIE_NAME not in set_cookie_names, (
                "register must never emit a Set-Cookie for tc_guest (AC-29)"
            )

            list_response = await client.get(BASE_CVS_URL)
        assert list_response.status_code == 200, list_response.text
        items = list_response.json()["items"]
        assert any(item["id"] == str(cv_id.value) for item in items), (
            "the guest's base CV must still be readable with the same cookie after registration"
        )

        after = dict(await _snapshot())
        assert after == before, f"the guest session row changed: {before} -> {after}"
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                guest_session_table.delete().where(guest_session_table.c.id == guest_session_id)
            )
        async with concurrent_app.state.engine.begin() as conn:
            await conn.execute(
                user_table.delete().where(user_table.c.email == EmailAddress.parse(email))
            )
