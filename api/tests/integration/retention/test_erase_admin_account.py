"""AC-11 (4.1, T7, test-after): an admin is erased exactly like a user, by the operator's
`erase-account` and by `POST /api/auth/delete-account`. The role leaves with the row and nothing
else holds it: no other table has a role column, and no output or log line mentions it.

The CLI half seeds a *committed* row (`erase_account` opens its own engine); the API half uses the
shared-session app and commits its seed, as `me_support.seed_user_and_sign_in` does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.retention import erase_account_command
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import (
    A_PASSWORD,
    DELETE_ACCOUNT_URL,
    assert_test_database,
    new_client,
    seed_user_and_sign_in,
)

_PHC = "$argon2id$v=19$m=8,t=1,p=1$c2FsdA$dummy"


@pytest.fixture(autouse=True)
def _configured(settings: Settings, clear_redis: None) -> None:
    configure_logging(settings)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with new_client(app) as c:
        yield c


async def _role_columns(session_or_engine: AsyncSession | AsyncEngine) -> list[str]:
    sql = text(
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = 'role'"
    )
    if isinstance(session_or_engine, AsyncEngine):
        async with session_or_engine.connect() as conn:
            return [r[0] for r in (await conn.execute(sql)).all()]
    return [r[0] for r in (await session_or_engine.execute(sql)).all()]


async def test_erase_account_erases_an_admin_and_no_other_table_holds_a_role(
    settings: Settings, engine: AsyncEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert_test_database(settings)
    user_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO identity_user (id, email, password_hash, created_at, "
                "password_updated_at, role) VALUES (:i, :e, :p, now(), now(), 'admin')"
            ),
            {"i": user_id, "e": f"erase-admin-{user_id}@example.com", "p": _PHC},
        )
    try:
        code = await erase_account_command.erase_account(
            settings, user_id=UserId(user_id), dry_run=False
        )

        assert code == erase_account_command.EXIT_OK
        out = capsys.readouterr()
        assert f"erased account {user_id}" in out.out
        assert "admin" not in out.out + out.err
        async with engine.connect() as conn:
            left = (
                await conn.execute(
                    text("SELECT count(*) FROM identity_user WHERE id = :i"), {"i": user_id}
                )
            ).scalar_one()
        assert left == 0
        assert await _role_columns(engine) == ["identity_user"]
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id})


async def test_delete_account_erases_an_admin_and_the_row_is_gone(
    client: AsyncClient,
    settings: Settings,
    session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token, user_id = await seed_user_and_sign_in(client, settings)
    await session.execute(
        text("UPDATE identity_user SET role = 'admin' WHERE id = :i"), {"i": user_id}
    )
    await session.commit()
    assert (
        await session.execute(text("SELECT role FROM identity_user WHERE id = :i"), {"i": user_id})
    ).scalar_one() == "admin"

    with caplog.at_level("INFO"):
        response = await client.post(
            DELETE_ACCOUNT_URL,
            json={"password": A_PASSWORD},
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": settings.public_base_url,
            },
        )

    assert response.status_code == 204, response.text
    assert (
        await session.execute(
            text("SELECT count(*) FROM identity_user WHERE id = :i"), {"i": user_id}
        )
    ).scalar_one() == 0
    assert await _role_columns(session) == ["identity_user"]
    assert "admin" not in " ".join(r.getMessage() for r in caplog.records)
