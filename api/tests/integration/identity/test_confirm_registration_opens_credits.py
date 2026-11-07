"""Application tests for `ConfirmRegistration`'s billing half (slice 5.2, T13 RED, AC-1, AC-27's
`sign_ups`, plan §0.6).

Confirming opens the `welcome` account after `users.add` and counts `sign_ups`, both on the same
session so one commit carries all three. A refusal anywhere before `users.add` opens and counts
nothing, and so does `users.add` itself refusing (the address became an account meanwhile).

That the three rows commit or vanish *together* needs the real adapters and is the T21/T22
persistence tests' to prove; the fakes here have no transaction.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tailorcraft.application.billing.open_credit_account import OpenCreditAccount
from tailorcraft.application.identity.confirm_registration import ConfirmRegistration
from tailorcraft.domain.billing.credit_account import CreditAccount
from tailorcraft.domain.billing.events import CreditAccountOpened
from tailorcraft.domain.billing.value_objects import CreditEntryKind, OpeningGrant
from tailorcraft.domain.identity.errors import ConfirmationTokenInvalid, EmailAlreadyRegistered
from tailorcraft.domain.identity.pending_registration import PendingRegistration
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, TokenHash
from tailorcraft.domain.shared.counters import DemandCounter
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeCreditAccountRepository,
    FakePendingRegistrationRepository,
    LoggingEventPublisher,
    LoggingUserRepository,
    RecordingDailyCounter,
)

_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZWhhc2g")
_EMAIL = EmailAddress.parse("alex@example.com")
_TOKEN = TokenHash(value="c" * 64)
_TTL = timedelta(hours=24)


class _LoggingCredits(FakeCreditAccountRepository):
    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self._log = log

    async def add(self, account: CreditAccount) -> None:
        self._log.append("credits.add")
        await super().add(account)


class _Rig:
    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self.log: list[str] = []
        self.pending = FakePendingRegistrationRepository(self.log)
        self.users = LoggingUserRepository(self.log)
        self.events = LoggingEventPublisher(self.log)
        self.credits = _LoggingCredits(self.log)
        self.counters = RecordingDailyCounter()
        self.use_case = ConfirmRegistration(
            self.pending,
            self.users,
            clock,
            self.events,
            open_account=OpenCreditAccount(self.credits, self.events),
            counters=self.counters,
        )

    def seed_issued(self, *, age: timedelta = timedelta(0)) -> None:
        p = PendingRegistration.request(
            self.pending.next_identity(), _EMAIL, _HASH, self.clock.now() - age, _TTL
        )
        p.issue(_TOKEN, self.clock.now() - age)
        self.pending.seed(p)


async def test_confirming_opens_the_new_users_account_with_one_welcome_credit(
    clock: FixedClock,
) -> None:
    rig = _Rig(clock)
    rig.seed_issued()

    user_id = await rig.use_case(_TOKEN)

    account = await rig.credits.find(user_id)
    assert account is not None
    assert account.balance == 1
    (entry,) = account.entries
    assert (entry.kind, entry.credits, entry.at) == (CreditEntryKind.WELCOME, 1, clock.now())


async def test_confirming_counts_one_sign_up_on_the_instants_utc_day(clock: FixedClock) -> None:
    rig = _Rig(clock)
    rig.seed_issued()

    await rig.use_case(_TOKEN)

    assert rig.counters.calls == [(DemandCounter.SIGN_UPS, clock.now().date())]


async def test_the_account_is_opened_and_the_sign_up_counted_after_the_user_is_added(
    clock: FixedClock,
) -> None:
    rig = _Rig(clock)
    rig.seed_issued()
    await rig.use_case(_TOKEN)

    assert "credits.add" in rig.log
    assert rig.log.index("credits.add") > rig.log.index("users.add")


async def test_the_opened_event_is_published_beside_the_registered_one(clock: FixedClock) -> None:
    rig = _Rig(clock)
    rig.seed_issued()

    await rig.use_case(_TOKEN)

    assert CreditAccountOpened(occurred_at=clock.now(), opening=OpeningGrant.WELCOME) in (
        rig.events.published
    )


async def test_an_address_that_became_an_account_meanwhile_opens_nothing_and_counts_nothing(
    clock: FixedClock,
) -> None:
    rig = _Rig(clock)
    rig.seed_issued()
    existing = User.register_with_password(rig.users.next_identity(), _EMAIL, _HASH, clock.now())
    existing.release_events()
    await rig.users.add(existing)

    with pytest.raises(EmailAlreadyRegistered):
        await rig.use_case(_TOKEN)

    assert await rig.credits.find(existing.id) is None
    assert rig.credits.save_calls == 0
    assert rig.counters.calls == []


@pytest.mark.parametrize("case", ["unknown", "expired"])
async def test_a_refused_token_opens_nothing_and_counts_nothing(
    case: str, clock: FixedClock
) -> None:
    rig = _Rig(clock)
    rig.seed_issued(age=_TTL if case == "expired" else timedelta(0))
    token = _TOKEN if case == "expired" else TokenHash(value="d" * 64)

    with pytest.raises(ConfirmationTokenInvalid):
        await rig.use_case(token)

    assert rig.credits.save_calls == 0
    assert rig.counters.calls == []
