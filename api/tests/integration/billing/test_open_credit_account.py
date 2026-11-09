"""Application tests for `OpenCreditAccount` (slice 5.2, T13 RED, AC-1, AC-2's opening kinds)."""

from __future__ import annotations

import pytest

from tailorcraft.application.billing.open_credit_account import (
    OpenCreditAccount,
    OpenCreditAccountCommand,
)
from tailorcraft.domain.billing.events import CreditAccountOpened
from tailorcraft.domain.billing.value_objects import CreditEntryKind, OpeningGrant
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.billing.support import a_user_id
from tests.integration.fakes import FakeCreditAccountRepository, RecordingEventPublisher


@pytest.mark.parametrize(
    ("opening", "kind"),
    [
        (OpeningGrant.WELCOME, CreditEntryKind.WELCOME),
        (OpeningGrant.EXISTING_ACCOUNT, CreditEntryKind.EXISTING_ACCOUNT),
    ],
)
async def test_opening_stores_one_account_with_one_credit_of_the_given_kind(
    opening: OpeningGrant, kind: CreditEntryKind, clock: FixedClock
) -> None:
    credits = FakeCreditAccountRepository()
    uid = a_user_id()

    await OpenCreditAccount(credits, RecordingEventPublisher())(
        OpenCreditAccountCommand(user_id=uid, opening=opening, at=clock.now())
    )

    account = await credits.find(uid)
    assert account is not None
    assert account.balance == 1
    (entry,) = account.entries
    assert (entry.kind, entry.credits, entry.run, entry.at) == (kind, 1, None, clock.now())


async def test_opening_publishes_the_opened_event_once_and_only_after_the_account_is_stored(
    clock: FixedClock,
) -> None:
    credits = FakeCreditAccountRepository()
    uid = a_user_id()
    stored_when_published: list[bool] = []

    class _Publisher:
        def __init__(self) -> None:
            self.published: list[DomainEvent] = []

        async def publish(self, *events: DomainEvent) -> None:
            stored_when_published.append(await credits.find(uid) is not None)
            self.published.extend(events)

    events = _Publisher()
    await OpenCreditAccount(credits, events)(
        OpenCreditAccountCommand(user_id=uid, opening=OpeningGrant.WELCOME, at=clock.now())
    )

    assert [type(e) for e in events.published] == [CreditAccountOpened]
    assert events.published == [
        CreditAccountOpened(occurred_at=clock.now(), opening=OpeningGrant.WELCOME)
    ]
    assert stored_when_published == [True]


async def test_opening_one_account_leaves_every_other_user_without_one(clock: FixedClock) -> None:
    credits = FakeCreditAccountRepository()
    uid, other = a_user_id(), a_user_id()

    await OpenCreditAccount(credits, RecordingEventPublisher())(
        OpenCreditAccountCommand(user_id=uid, opening=OpeningGrant.WELCOME, at=clock.now())
    )

    assert await credits.find(other) is None
