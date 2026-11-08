"""`grant-credits` -- the CLI contract (slice 5.2, T30, AC-30), test-after.

Drives the seam `grant_credits_command.grant_credits(settings, ...)` with the already-swapped `settings`
fixture (`get_settings()` under `APP_ENV=test` names the **dev** database, so nothing here calls
`run_from_cli`). Rows are seeded and read through committed connections of the suite's `engine`, because
the command builds its own engine and a rolled-back `session` would be invisible to it
(`identity/test_role_cli.py`'s reason).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.cli import main
from tailorcraft.domain.billing.credit_account import CreditAccount
from tailorcraft.domain.billing.value_objects import CreditEntryId, GrantAmount, OpeningGrant
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, UserId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.infrastructure.billing import grant_credits_command
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.repositories.billing.credit_account import (
    SqlAlchemyCreditAccountRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.integration.claim_race_support import assert_test_database


@pytest.fixture(autouse=True)
def _configured_logging(settings: Settings) -> None:
    configure_logging(settings)


class _Accounts:
    def __init__(self, engine: AsyncEngine, clock: Clock) -> None:
        self.engine = engine
        self.clock = clock
        self.ids: list[UserId] = []

    async def seed(self, *, balance: int = 1, with_account: bool = True, email: str = "") -> UserId:
        """A committed user, and (unless `with_account=False`) a credit account holding `balance`."""
        user_id = UserId(uuid4())
        user = User.register_with_password(
            user_id,
            EmailAddress.parse(email or f"grant-{user_id.value}@example.com"),
            PasswordHash("$argon2id$v=19$m=8,t=1,p=1$c2FsdA$dummy"),
            self.clock.now(),
        )
        user.release_events()
        async with async_sessionmaker(self.engine, expire_on_commit=False)() as session:
            session.add(user)
            if with_account:
                credits = SqlAlchemyCreditAccountRepository(session)
                account = CreditAccount.open(
                    user_id=user_id,
                    opening=OpeningGrant.WELCOME,
                    entry_id=credits.next_entry_identity(),
                    at=self.clock.now(),
                )
                if balance > 1:
                    account.grant(
                        GrantAmount(balance - 1),
                        entry_id=CreditEntryId(uuid4()),
                        at=self.clock.now(),
                    )
                await session.flush()
                await credits.add(account)
            await session.commit()
        self.ids.append(user_id)
        return user_id

    async def balance(self, user_id: UserId) -> int:
        """Read from a fresh connection: what another process would see."""
        async with self.engine.connect() as conn:
            return int(
                (
                    await conn.execute(
                        text(
                            "SELECT COALESCE(SUM(credits), 0) FROM billing_credit_entry "
                            "WHERE user_id = :u"
                        ),
                        {"u": user_id.value},
                    )
                ).scalar_one()
            )


@pytest_asyncio.fixture
async def accounts(engine: AsyncEngine, clock: Clock) -> AsyncIterator[_Accounts]:
    rig = _Accounts(engine, clock)
    try:
        yield rig
    finally:
        async with engine.begin() as conn:
            await conn.execute(user_table.delete().where(user_table.c.id.in_(rig.ids)))


async def _run(settings: Settings, user_id: UserId, credits: int, *, dry_run: bool = False) -> int:
    assert_test_database(settings)
    return await grant_credits_command.grant_credits(
        settings, user_id=user_id, amount=GrantAmount(credits), dry_run=dry_run
    )


async def test_a_grant_adds_the_credits_prints_the_line_and_logs_the_event(
    settings: Settings,
    accounts: _Accounts,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    uid = await accounts.seed(balance=2)

    with caplog.at_level(logging.INFO):
        code = await _run(settings, uid, 5)

    assert code == grant_credits_command.EXIT_OK
    captured = capsys.readouterr()
    assert captured.out.strip() == f"granted user {uid.value}: 5 credit(s), balance 2 → 7"
    assert await accounts.balance(uid) == 7
    assert grant_credits_command.EVENT_GRANT in caplog.text
    assert "CreditsGranted" in caplog.text, "positive control for the dry run's absence"


async def test_a_dry_run_prints_the_would_line_and_changes_nothing(
    settings: Settings,
    accounts: _Accounts,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    uid = await accounts.seed(balance=1)

    with caplog.at_level(logging.INFO):
        code = await _run(settings, uid, 3, dry_run=True)

    assert code == 0
    assert (
        capsys.readouterr().out.strip()
        == f"would grant user {uid.value}: 3 credit(s), balance 1 → 4 (dry run)"
    )
    assert await accounts.balance(uid) == 1
    assert "CreditsGranted" not in caplog.text, "a dry run publishes no event"


async def test_an_unknown_id_exits_1_with_the_sentence_and_grants_nothing(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = UserId(uuid4())

    code = await _run(settings, missing, 1)

    assert code == grant_credits_command.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == f"grant-credits: no account with id {missing.value}."


async def test_a_user_without_a_credit_account_exits_1_and_creates_none(
    settings: Settings, accounts: _Accounts, capsys: pytest.CaptureFixture[str]
) -> None:
    uid = await accounts.seed(with_account=False)

    code = await _run(settings, uid, 1)

    assert code == 1
    assert capsys.readouterr().err.strip() == f"grant-credits: no account with id {uid.value}."
    assert await accounts.balance(uid) == 0


async def test_a_foreign_database_exits_1_before_any_read(
    settings: Settings,
    accounts: _Accounts,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tailorcraft.infrastructure.persistence import database_guard

    uid = await accounts.seed(balance=1)

    async def _refuse(_session: object, _settings: object) -> None:
        raise database_guard.ForeignDatabase("intended_db", "actual_db")

    monkeypatch.setattr(grant_credits_command, "refuse_a_foreign_database", _refuse)

    with caplog.at_level(logging.WARNING):
        code = await _run(settings, uid, 4)

    assert code == 1
    err = capsys.readouterr().err
    assert "intended_db" in err
    assert "actual_db" in err
    assert grant_credits_command.EVENT_GRANT_FAILED in caplog.text
    assert await accounts.balance(uid) == 1


async def test_a_database_failure_exits_1_naming_only_the_error_type(
    settings: Settings, accounts: _Accounts, capsys: pytest.CaptureFixture[str]
) -> None:
    uid = await accounts.seed(balance=1)
    broken = settings.model_copy(
        update={"database_url": "postgresql+asyncpg://nobody:nope@127.0.0.1:1/tailorcraft_test"}
    )

    code = await _run(broken, uid, 1)

    assert code == 1
    err = capsys.readouterr().err.strip()
    assert err.startswith("grant-credits: failed (")
    assert err.endswith(").")
    assert "nope" not in err
    assert await accounts.balance(uid) == 1


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--user-id", "not-a-uuid", "--credits", "1"],
        ["--user-id", str(uuid4())],
        ["--user-id", str(uuid4()), "--credits", "0"],
        ["--user-id", str(uuid4()), "--credits", "101"],
        ["--user-id", str(uuid4()), "--credits", "-3"],
        ["--user-id", str(uuid4()), "--credits", "many"],
    ],
)
def test_missing_malformed_or_out_of_range_arguments_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["grant-credits", *argv])
    assert exc_info.value.code == 2


async def test_the_bounds_one_and_one_hundred_are_accepted(
    settings: Settings, accounts: _Accounts
) -> None:
    uid = await accounts.seed(balance=1)

    assert await _run(settings, uid, 1) == 0
    assert await _run(settings, uid, 100) == 0
    assert await accounts.balance(uid) == 102


async def test_output_and_logs_carry_the_id_and_counts_never_the_email(
    settings: Settings,
    accounts: _Accounts,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = uuid4().hex
    uid = await accounts.seed(email=f"grant-marker-{marker}@example.com")

    with caplog.at_level(logging.DEBUG):
        assert await _run(settings, uid, 2, dry_run=True) == 0
        assert await _run(settings, uid, 2) == 0
        assert await _run(settings, UserId(uuid4()), 2) == 1

    captured = capsys.readouterr()
    for name, text_ in (("stdout", captured.out), ("stderr", captured.err), ("logs", caplog.text)):
        assert marker not in text_, f"the email marker reached {name}"
    assert str(uid.value) in captured.out
    assert str(uid.value) in caplog.text, (
        "positive control: the id is logged, so the absence counts"
    )
