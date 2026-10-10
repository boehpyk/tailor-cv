"""`grant-role` / `revoke-role` — the CLI contract (slice 4.1, T13; AC-26…AC-29), test-after.

Every test drives the seam `role_command.change_role(settings, …)` with the already-swapped
`settings` fixture: `get_settings()` under `APP_ENV=test` names the **dev** database (CLAUDE.md, 1.4),
so nothing here calls `run_from_cli`. Rows are seeded and read through committed connections of the
suite's `engine`, because `change_role` builds its own engine and a rolled-back `session` would be
invisible to it. Races follow `claim_race_support`: lock_timeout pinned, overlap proven from
`pg_stat_activity`, every task awaited under `wait_for`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.cli import main
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, Role, UserId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.infrastructure.identity import role_command
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.settings import Settings
from tests.integration.claim_race_support import (
    assert_test_database,
    pinned_session,
    wait_for_lock_waiter,
)

_STEP_TIMEOUT = 15.0
GRANT = "grant-role"
REVOKE = "revoke-role"


@pytest.fixture(autouse=True)
def _configured_logging(settings: Settings) -> None:
    configure_logging(settings)


@pytest_asyncio.fixture
async def users(engine: AsyncEngine, clock: Clock) -> AsyncIterator[_Users]:
    rig = _Users(engine, clock)
    try:
        yield rig
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL lock_timeout = '8000ms'"))
            await conn.execute(user_table.delete().where(user_table.c.id.in_(rig.ids)))


class _Users:
    def __init__(self, engine: AsyncEngine, clock: Clock) -> None:
        self.engine = engine
        self.clock = clock
        self.ids: list[UserId] = []

    async def seed(self, email: str | None = None) -> UserId:
        user_id = UserId(uuid4())
        user = User.register_with_password(
            user_id,
            EmailAddress.parse(email or f"role-{user_id.value}@example.com"),
            PasswordHash("$argon2id$v=19$m=8,t=1,p=1$c2FsdA$dummy"),
            self.clock.now(),
        )
        user.release_events()
        async with async_sessionmaker(self.engine, expire_on_commit=False)() as session:
            session.add(user)
            await session.commit()
        self.ids.append(user_id)
        return user_id

    async def role(self, user_id: UserId) -> str | None:
        """Read from a fresh connection: what another process would see."""
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(select(user_table.c.role).where(user_table.c.id == user_id))
            ).scalar_one_or_none()
            return None if row is None else Role(row).value


async def _run(settings: Settings, user_id: UserId, command: str, *, dry_run: bool = False) -> int:
    assert_test_database(settings)
    return await role_command.change_role(
        settings,
        user_id=user_id,
        to=Role.ADMIN if command == GRANT else Role.USER,
        dry_run=dry_run,
        command=command,
    )


# --- AC-26 / AC-27: output, exit codes ------------------------------------------------------------


async def test_grant_changes_the_row_prints_the_line_and_logs_the_event(
    settings: Settings,
    users: _Users,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    uid = await users.seed()
    with caplog.at_level(logging.INFO):
        code = await _run(settings, uid, GRANT)

    assert code == role_command.EXIT_OK
    assert capsys.readouterr().out.strip() == f"user {uid.value}: role user → admin"
    assert await users.role(uid) == "admin"
    assert "UserRoleChanged" in caplog.text
    assert "from_role" in caplog.text
    assert role_command.EVENT_ROLE_CHANGE in caplog.text


async def test_grant_to_an_admin_changes_nothing_and_logs_no_event(
    settings: Settings,
    users: _Users,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    uid = await users.seed()
    assert await _run(settings, uid, GRANT) == 0
    capsys.readouterr()
    caplog.clear()

    with caplog.at_level(logging.INFO):
        code = await _run(settings, uid, GRANT)

    assert code == 0
    assert (
        capsys.readouterr().out.strip() == f"user {uid.value}: role already admin; nothing changed"
    )
    assert "UserRoleChanged" not in caplog.text
    assert await users.role(uid) == "admin"


async def test_grant_dry_run_reports_and_leaves_the_row_unchanged(
    settings: Settings, users: _Users, capsys: pytest.CaptureFixture[str]
) -> None:
    uid = await users.seed()

    code = await _run(settings, uid, GRANT, dry_run=True)

    assert code == 0
    assert (
        capsys.readouterr().out.strip()
        == f"would change user {uid.value}: role user → admin (dry run)"
    )
    assert await users.role(uid) == "user"


async def test_grant_dry_run_on_an_admin_says_nothing_would_change(
    settings: Settings, users: _Users, capsys: pytest.CaptureFixture[str]
) -> None:
    uid = await users.seed()
    await _run(settings, uid, GRANT)
    capsys.readouterr()

    code = await _run(settings, uid, GRANT, dry_run=True)

    assert code == 0
    assert (
        capsys.readouterr().out.strip()
        == f"user {uid.value}: role already admin; nothing would change (dry run)"
    )
    assert await users.role(uid) == "admin"


async def test_revoke_changes_an_admin_to_user(
    settings: Settings, users: _Users, capsys: pytest.CaptureFixture[str]
) -> None:
    uid = await users.seed()
    await _run(settings, uid, GRANT)
    capsys.readouterr()

    code = await _run(settings, uid, REVOKE)

    assert code == 0
    assert capsys.readouterr().out.strip() == f"user {uid.value}: role admin → user"
    assert await users.role(uid) == "user"


async def test_revoke_from_a_user_changes_nothing(
    settings: Settings, users: _Users, capsys: pytest.CaptureFixture[str]
) -> None:
    uid = await users.seed()

    code = await _run(settings, uid, REVOKE)

    assert code == 0
    assert (
        capsys.readouterr().out.strip() == f"user {uid.value}: role already user; nothing changed"
    )


async def test_revoke_dry_run_leaves_an_admin_in_place(
    settings: Settings, users: _Users, capsys: pytest.CaptureFixture[str]
) -> None:
    uid = await users.seed()
    await _run(settings, uid, GRANT)
    capsys.readouterr()

    code = await _run(settings, uid, REVOKE, dry_run=True)

    assert code == 0
    assert (
        capsys.readouterr().out.strip()
        == f"would change user {uid.value}: role admin → user (dry run)"
    )
    assert await users.role(uid) == "admin"


async def test_revoking_the_last_admin_is_allowed_without_a_warning(
    settings: Settings, users: _Users, engine: AsyncEngine, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-27: the only admin in the test database is revoked: exit 0, nothing on stderr."""
    uid = await users.seed()
    await _run(settings, uid, GRANT)
    async with engine.connect() as conn:
        others = (
            await conn.execute(
                select(user_table.c.id).where(
                    user_table.c.role == Role.ADMIN, user_table.c.id != uid
                )
            )
        ).all()
    # Other admins cannot exist in a clean test database; if a stray one does, the claim "last
    # admin" is not what was exercised, so fail loudly rather than pass for the wrong reason.
    assert others == []
    capsys.readouterr()

    code = await _run(settings, uid, REVOKE)

    assert code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == f"user {uid.value}: role admin → user"
    assert captured.err == ""
    assert await users.role(uid) == "user"


@pytest.mark.parametrize("command", [GRANT, REVOKE])
async def test_an_unknown_id_exits_1_with_the_sentence(
    settings: Settings, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = UserId(uuid4())

    code = await _run(settings, missing, command)

    assert code == role_command.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == f"{command}: no account with id {missing.value}."


@pytest.mark.parametrize("command", [GRANT, REVOKE])
async def test_a_foreign_database_exits_1_before_any_read(
    settings: Settings,
    users: _Users,
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tailorcraft.infrastructure.persistence import database_guard

    uid = await users.seed()

    async def _refuse(_session: object, _settings: object) -> None:
        raise database_guard.ForeignDatabase("intended_db", "actual_db")

    monkeypatch.setattr(role_command, "refuse_a_foreign_database", _refuse)

    with caplog.at_level(logging.WARNING):
        code = await _run(settings, uid, command)

    assert code == 1
    err = capsys.readouterr().err
    assert "intended_db" in err
    assert "actual_db" in err
    assert role_command.EVENT_ROLE_CHANGE_FAILED in caplog.text
    assert await users.role(uid) == "user"


async def test_a_database_failure_exits_1_naming_only_the_error_type(
    settings: Settings, users: _Users, capsys: pytest.CaptureFixture[str]
) -> None:
    uid = await users.seed()
    broken = settings.model_copy(
        update={"database_url": "postgresql+asyncpg://nobody:nope@127.0.0.1:1/tailorcraft_test"}
    )

    code = await _run(broken, uid, GRANT)

    assert code == 1
    err = capsys.readouterr().err.strip()
    assert err.startswith(f"{GRANT}: failed (")
    assert err.endswith(").")
    assert "nope" not in err
    assert await users.role(uid) == "user"


@pytest.mark.parametrize("command", [GRANT, REVOKE])
@pytest.mark.parametrize("argv", [[], ["--user-id", "not-a-uuid"]])
def test_missing_or_malformed_user_id_exits_2(command: str, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main([command, *argv])
    assert exc_info.value.code == 2


# --- AC-28: privacy ---------------------------------------------------------------------------


async def test_a_real_grant_revoke_and_dry_run_never_print_or_log_the_email(
    settings: Settings,
    users: _Users,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = uuid4().hex
    email = f"role-marker-{marker}@example.com"
    uid = await users.seed(email)

    with caplog.at_level(logging.DEBUG):
        assert await _run(settings, uid, GRANT, dry_run=True) == 0
        assert await _run(settings, uid, GRANT) == 0
        assert await _run(settings, uid, REVOKE) == 0

    captured = capsys.readouterr()
    for name, text_ in (("stdout", captured.out), ("stderr", captured.err), ("logs", caplog.text)):
        assert marker not in text_, f"the email marker reached {name}"
    # Positive control: the id is in stdout and in the logs, so the absences are not vacuous.
    assert str(uid.value) in captured.out
    assert str(uid.value) in caplog.text


async def test_a_failed_run_names_the_id_but_not_the_email(
    settings: Settings,
    users: _Users,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = uuid4().hex
    uid = await users.seed(f"role-marker-{marker}@example.com")
    broken = settings.model_copy(
        update={"database_url": "postgresql+asyncpg://nobody:nope@127.0.0.1:1/tailorcraft_test"}
    )

    with caplog.at_level(logging.DEBUG):
        assert await _run(broken, uid, GRANT) == 1

    captured = capsys.readouterr()
    assert marker not in captured.err + captured.out + caplog.text
    assert str(uid.value) in caplog.text


# --- AC-29: races, on two real connections ----------------------------------------------------


async def _lock_waiters(engine: AsyncEngine, count: int, within: float = 5.0) -> None:
    """Until `count` backends are waiting on a lock in a `FOR UPDATE` on `identity_user`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    async with engine.connect() as probe:
        while loop.time() < deadline:
            n = (
                await probe.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock' "
                        "AND query ILIKE '%identity_user%for update%'"
                    )
                )
            ).scalar_one()
            if n >= count:
                return
            await probe.rollback()
            await asyncio.sleep(0.02)
    raise AssertionError(f"fewer than {count} commands were ever waiting on the user row lock")


async def test_ac29a_a_grant_waiting_on_an_erasures_lock_finds_the_account_gone(
    settings: Settings,
    users: _Users,
    engine: AsyncEngine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uid = await users.seed()
    async with pinned_session(engine) as eraser:
        # What `erase-account` does to the row: lock it `FOR UPDATE`, then delete.
        await eraser.execute(
            text("SELECT id FROM identity_user WHERE id = :u FOR UPDATE"), {"u": uid.value}
        )
        grant = asyncio.create_task(_run(settings, uid, GRANT))
        await wait_for_lock_waiter(engine, "identity_user", "for update")
        assert not grant.done(), "the grant did not wait on the erasure's lock"
        await eraser.execute(text("DELETE FROM identity_user WHERE id = :u"), {"u": uid.value})
        await eraser.commit()
        code = await asyncio.wait_for(grant, _STEP_TIMEOUT)

    assert code == 1
    assert capsys.readouterr().err.strip() == f"{GRANT}: no account with id {uid.value}."
    assert await users.role(uid) is None


async def test_ac29b_a_concurrent_grant_and_revoke_serialize_and_each_states_its_own_transition(
    settings: Settings,
    users: _Users,
    engine: AsyncEngine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uid = await users.seed()
    async with pinned_session(engine) as holder:
        await holder.execute(
            text("SELECT id FROM identity_user WHERE id = :u FOR UPDATE"), {"u": uid.value}
        )
        grant = asyncio.create_task(_run(settings, uid, GRANT))
        await _lock_waiters(engine, 1)
        revoke = asyncio.create_task(_run(settings, uid, REVOKE))
        await _lock_waiters(engine, 2)  # both are queued behind the lock: the overlap is real
        assert not grant.done()
        assert not revoke.done()
        await holder.rollback()
        codes = [
            await asyncio.wait_for(grant, _STEP_TIMEOUT),
            await asyncio.wait_for(revoke, _STEP_TIMEOUT),
        ]

    assert codes == [0, 0]
    out = capsys.readouterr().out
    # The grant (queued first) read `user` and made user → admin; the revoke then read `admin` — not
    # the stale `user` it would have read without the lock, which would print "already user".
    assert f"user {uid.value}: role user → admin" in out
    assert f"user {uid.value}: role admin → user" in out
    assert "already" not in out
    assert await users.role(uid) == "user"  # the later commit's
