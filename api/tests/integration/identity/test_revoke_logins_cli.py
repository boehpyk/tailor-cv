"""`tailorcraft.cli revoke-logins --all [--dry-run]` — the break-glass CLI contract (T33, AC-13,
OQ-5), test-after: `infrastructure/identity/revoke_logins_command.py` was implemented before this
file existed (task-list: "api-dev half done 2026-09-24"), so there is no red to record — matching
`test_purge_cli.py`'s own note for the same reason.

**The one rule that dominates this file, CLAUDE.md's 1.4 incident.** `get_settings()` under
`APP_ENV=test` still returns the **dev** `database_url` — only the `settings` fixture swaps in
`test_database_url`. `run_from_cli` calls `get_settings()` itself, so no test here calls it: every
test drives the seam the module docstring names, `revoke_logins(settings, dry_run=)`, with the
already-swapped `settings` fixture. `_assert_test_database` asserts `"_test"` is in that URL before
the first statement, in every test that deletes.

**Why the rows are seeded through a genuinely committed connection, not the rolled-back `session`
fixture.** `revoke_logins` builds its own engine and its own session factory inside the function
(the module docstring: "the engine is built here, inside the loop `asyncio.run` opened"), which is a
different connection than any SAVEPOINT-per-test session could ever see under READ COMMITTED —
`test_purge_cli.py`'s `_CommittedRows` is the same shape for the same reason.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.cli import main
from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, LoginId, PasswordHash, UserId
from tailorcraft.infrastructure.api.refresh_cookie import mint_refresh_token
from tailorcraft.infrastructure.identity import revoke_logins_command
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.persistence.mapping.identity.login import login_table
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.settings import Settings

_A_TIMEDELTA_DAYS = 30


def _assert_test_database(settings: Settings) -> None:
    assert "_test" in settings.database_url, (
        "refusing to run a deleting revoke-logins test against a URL that is not the test "
        f"database: {settings.database_url!r}"
    )


@pytest.fixture(autouse=True)
def _configured_logging(settings: Settings) -> None:
    """`revoke_logins` is called directly in this file, never through `run_from_cli`, so nothing
    else here configures structlog — `test_purge_cli.py`'s identical fixture, same reason."""
    configure_logging(settings)


@dataclass(slots=True)
class _CommittedLogins:
    """Real, committed `User`+`Login` pairs, through a connection independent of the seam under
    test, deleted at teardown (whatever survives — a successful non-dry-run leaves nothing to
    delete, and `cleanup` tolerates that)."""

    engine: AsyncEngine
    _user_ids: list[UserId] = field(default_factory=list)

    async def seed(self, count: int) -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        factory = async_sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            for _ in range(count):
                user_id = UserId(uuid4())
                user = User.register_with_password(
                    user_id,
                    EmailAddress.parse(f"revoke-cli-{uuid4().hex}@example.com"),
                    PasswordHash("$argon2id$v=19$m=8,t=1,p=1$c2FsdA$dummy"),
                    now,
                )
                session.add(user)
                await session.flush()
                minted = mint_refresh_token()
                login = Login.start(
                    LoginId(uuid4()),
                    user_id,
                    minted.token_hash,
                    now,
                    timedelta(days=_A_TIMEDELTA_DAYS),
                )
                session.add(login)
                self._user_ids.append(user_id)
            await session.commit()

    async def login_count(self) -> int:
        async with self.engine.connect() as conn:
            result = await conn.execute(select(func.count()).select_from(login_table))
            return result.scalar_one()

    async def cleanup(self) -> None:
        if not self._user_ids:
            return
        async with self.engine.begin() as conn:
            await conn.execute(user_table.delete().where(user_table.c.id.in_(self._user_ids)))


@pytest_asyncio.fixture
async def committed_logins(engine: AsyncEngine) -> AsyncIterator[_CommittedLogins]:
    rig = _CommittedLogins(engine=engine)
    try:
        yield rig
    finally:
        await rig.cleanup()


# --- --all --dry-run: reports, deletes nothing ------------------------------------------------


async def test_dry_run_reports_the_count_and_deletes_nothing(
    settings: Settings, committed_logins: _CommittedLogins, capsys: pytest.CaptureFixture[str]
) -> None:
    _assert_test_database(settings)
    await committed_logins.seed(3)
    before = await committed_logins.login_count()
    assert before >= 3

    exit_code = await revoke_logins_command.revoke_logins(settings, dry_run=True)

    assert exit_code == revoke_logins_command.EXIT_OK
    out = capsys.readouterr().out
    assert f"would revoke {before} logins (dry run)" in out
    assert await committed_logins.login_count() == before


async def test_dry_run_uses_the_singular_noun_for_exactly_one(
    settings: Settings, committed_logins: _CommittedLogins, capsys: pytest.CaptureFixture[str]
) -> None:
    """The module's own promise: `noun = "login" if count == 1 else "logins"`. Proved against a table
    that genuinely holds exactly one row, not by trusting the plural case to generalise."""
    _assert_test_database(settings)
    # Clear whatever this run's own login table already holds is not possible without touching
    # other tests' data, so this test seeds one and asserts the delta reads "N+1" — no: dry run
    # reports the WHOLE table's count, so the singular claim can only be tested honestly against an
    # empty table plus one seed. `revoke_logins` itself is what empties the table (dry_run=False),
    # so this test does exactly that first, then seeds one.
    await revoke_logins_command.revoke_logins(settings, dry_run=False)
    capsys.readouterr()  # discard the cleanup call's own "revoked N logins" line
    assert await committed_logins.login_count() == 0
    await committed_logins.seed(1)
    assert await committed_logins.login_count() == 1

    exit_code = await revoke_logins_command.revoke_logins(settings, dry_run=True)

    assert exit_code == revoke_logins_command.EXIT_OK
    out = capsys.readouterr().out
    assert "would revoke 1 login (dry run)" in out
    assert "logins" not in out
    assert await committed_logins.login_count() == 1


# --- --all: deletes everything, singular/plural on the real path -------------------------------


async def test_all_deletes_every_login_and_reports_the_count(
    settings: Settings, committed_logins: _CommittedLogins, capsys: pytest.CaptureFixture[str]
) -> None:
    _assert_test_database(settings)
    await revoke_logins_command.revoke_logins(settings, dry_run=False)
    capsys.readouterr()  # discard the cleanup call's own "revoked N logins" line
    assert await committed_logins.login_count() == 0
    await committed_logins.seed(2)
    assert await committed_logins.login_count() == 2

    exit_code = await revoke_logins_command.revoke_logins(settings, dry_run=False)

    assert exit_code == revoke_logins_command.EXIT_OK
    out = capsys.readouterr().out
    assert "revoked 2 logins" in out
    assert await committed_logins.login_count() == 0


async def test_all_uses_the_singular_noun_for_exactly_one_real_deletion(
    settings: Settings, committed_logins: _CommittedLogins, capsys: pytest.CaptureFixture[str]
) -> None:
    _assert_test_database(settings)
    await revoke_logins_command.revoke_logins(settings, dry_run=False)
    capsys.readouterr()  # discard the cleanup call's own "revoked N logins" line
    assert await committed_logins.login_count() == 0
    await committed_logins.seed(1)

    exit_code = await revoke_logins_command.revoke_logins(settings, dry_run=False)

    assert exit_code == revoke_logins_command.EXIT_OK
    out = capsys.readouterr().out
    assert "revoked 1 login" in out
    assert "logins" not in out
    assert await committed_logins.login_count() == 0


# --- argparse's own usage exit: no --all -> exit 2 ----------------------------------------------


def test_bare_revoke_logins_without_all_exits_2() -> None:
    """AC-13/OQ-5's whole safety: `revoke-logins` typed by itself must be a usage error, never a
    mass sign-out. Through the real `cli.main`, no database and no Redis."""
    with pytest.raises(SystemExit) as exc_info:
        main(["revoke-logins"])
    assert exc_info.value.code == 2


# --- A database failure: exit 1, only the exception's type ---------------------------------------


async def test_a_database_failure_exits_1_and_logs_only_the_error_type(
    settings: Settings, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    _assert_test_database(settings)
    bad_settings = settings.model_copy(
        update={
            "database_url": (
                "postgresql+asyncpg://baduser:badpass@127.0.0.1:1/tailorcraft_test_unreachable"
            )
        }
    )

    with caplog.at_level(logging.WARNING):
        exit_code = await revoke_logins_command.revoke_logins(bad_settings, dry_run=False)

    assert exit_code == revoke_logins_command.EXIT_FAILED
    err = capsys.readouterr().err
    assert err.startswith("revoke-logins: failed (")
    assert err.rstrip().endswith(")."), err
    assert "identity.logins_revoke_failed" in caplog.text
    assert "error_type" in caplog.text
    # Never the DSN, the host, or any part of the connection string that was refused.
    assert "baduser" not in caplog.text
    assert "badpass" not in caplog.text
    assert "127.0.0.1" not in caplog.text


# --- The `_assert_test_database` guard runs before the first statement, in every test above ------
