"""RED (T6): the billing domain, from the spec's CE-1..3 and CR-1..6. Pure: no I/O, no fixtures."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from tailorcraft.domain.billing.credit_account import CreditAccount, CreditEntry
from tailorcraft.domain.billing.errors import NoCreditsLeft, RunAlreadyCharged
from tailorcraft.domain.billing.events import (
    CreditAccountOpened,
    CreditCharged,
    CreditRefunded,
    CreditsGranted,
    PackClicked,
    PaymentNoticeChosen,
)
from tailorcraft.domain.billing.value_objects import (
    ChargedRunRef,
    CreditEntryId,
    CreditEntryKind,
    CreditNotice,
    GrantAmount,
    OpeningGrant,
    Pack,
)
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.errors import InvariantViolated

_AT = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
_LATER = _AT + timedelta(minutes=5)
_USER = UserId(UUID("55555555-5555-7555-8555-555555555555"))
_RUN_A = ChargedRunRef(UUID("aaaaaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"))
_RUN_B = ChargedRunRef(UUID("bbbbbbbb-bbbb-7bbb-8bbb-bbbbbbbbbbbb"))


def _eid(n: int) -> CreditEntryId:
    return CreditEntryId(UUID(int=n))


def _open(opening: OpeningGrant = OpeningGrant.WELCOME) -> CreditAccount:
    return CreditAccount.open(user_id=_USER, opening=opening, entry_id=_eid(1), at=_AT)


# ---- CE-1 / CE-2 / CE-3: the entry ---------------------------------------------------------------


def test_opening_entries_are_plus_one_with_no_run() -> None:
    for grant, kind in (
        (OpeningGrant.WELCOME, CreditEntryKind.WELCOME),
        (OpeningGrant.EXISTING_ACCOUNT, CreditEntryKind.EXISTING_ACCOUNT),
    ):
        e = CreditEntry.opening(id=_eid(1), grant=grant, at=_AT)
        assert (e.kind, e.credits, e.run, e.at, e.id) == (kind, 1, None, _AT, _eid(1))


@pytest.mark.parametrize("n", [1, 7, 100])
def test_a_granted_entry_carries_the_amount_and_no_run(n: int) -> None:
    e = CreditEntry.granted(id=_eid(2), amount=GrantAmount(n), at=_AT)
    assert (e.kind, e.credits, e.run) == (CreditEntryKind.GRANT, n, None)


def test_a_charge_entry_is_minus_one_and_names_its_run() -> None:
    e = CreditEntry.charge(id=_eid(2), run=_RUN_A, at=_AT)
    assert (e.kind, e.credits, e.run) == (CreditEntryKind.CHARGE, -1, _RUN_A)


def test_a_refund_entry_is_plus_one_and_names_its_run() -> None:
    e = CreditEntry.refund(id=_eid(2), run=_RUN_A, at=_AT)
    assert (e.kind, e.credits, e.run) == (CreditEntryKind.REFUND, 1, _RUN_A)


@pytest.mark.parametrize(
    "bad",
    [
        datetime(2026, 11, 7, 12, 0, 0),  # naive
        datetime(2026, 11, 7, 12, 0, 0, 500, tzinfo=UTC),  # sub-second
    ],
    ids=["naive", "sub_second"],
)
def test_every_entry_constructor_refuses_a_bad_instant(bad: datetime) -> None:
    makers: tuple[Callable[[], CreditEntry], ...] = (
        lambda: CreditEntry.opening(id=_eid(1), grant=OpeningGrant.WELCOME, at=bad),
        lambda: CreditEntry.granted(id=_eid(1), amount=GrantAmount(1), at=bad),
        lambda: CreditEntry.charge(id=_eid(1), run=_RUN_A, at=bad),
        lambda: CreditEntry.refund(id=_eid(1), run=_RUN_A, at=bad),
    )
    for make in makers:
        with pytest.raises(InvariantViolated):
            make()


def test_an_aware_whole_second_instant_in_another_zone_is_accepted() -> None:
    at = datetime(2026, 11, 7, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert CreditEntry.charge(id=_eid(1), run=_RUN_A, at=at).at == at


# ---- Value objects -------------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 50, 100])
def test_grant_amount_accepts_one_to_one_hundred(n: int) -> None:
    assert GrantAmount(n).value == n


@pytest.mark.parametrize("n", [0, -1, 101, 1000])
def test_grant_amount_refuses_out_of_range(n: int) -> None:
    from tailorcraft.domain.billing.errors import InvalidGrantAmount

    with pytest.raises(InvalidGrantAmount):
        GrantAmount(n)


def test_grant_amount_refuses_a_bool() -> None:
    from tailorcraft.domain.billing.errors import InvalidGrantAmount

    with pytest.raises(InvalidGrantAmount):
        GrantAmount(True)


def test_pack_values_are_the_priced_bundles() -> None:
    assert (Pack.PACK_3.credits, Pack.PACK_3.price_cents) == (3, 500)
    assert (Pack.PACK_10.credits, Pack.PACK_10.price_cents) == (10, 1200)
    assert {p.value for p in Pack} == {"pack_3", "pack_10"}


# ---- Opening, CR-1, balance, notice --------------------------------------------------------------


def test_opening_an_account_has_one_opening_entry_and_a_balance_of_one() -> None:
    acct = _open()
    assert acct.user_id == _USER
    assert [e.kind for e in acct.entries] == [CreditEntryKind.WELCOME]
    assert acct.balance == 1


def test_opening_records_one_event_naming_the_opening_kind() -> None:
    acct = _open(OpeningGrant.EXISTING_ACCOUNT)
    assert acct.release_events() == (
        CreditAccountOpened(occurred_at=_AT, opening=OpeningGrant.EXISTING_ACCOUNT),
    )


def test_opening_entry_stays_first_after_later_entries() -> None:
    acct = _open()
    acct.grant(GrantAmount(2), entry_id=_eid(2), at=_LATER)
    acct.charge(_RUN_A, entry_id=_eid(3), at=_LATER)
    assert [e.kind for e in acct.entries] == [
        CreditEntryKind.WELCOME,
        CreditEntryKind.GRANT,
        CreditEntryKind.CHARGE,
    ]
    assert [e.kind for e in acct.entries].count(CreditEntryKind.WELCOME) == 1


def test_open_refuses_a_bad_instant() -> None:
    with pytest.raises(InvariantViolated):
        CreditAccount.open(
            user_id=_USER,
            opening=OpeningGrant.WELCOME,
            entry_id=_eid(1),
            at=datetime(2026, 11, 7, 12, 0, 0),
        )


def test_balance_is_the_sum_of_all_entries() -> None:
    acct = _open()
    acct.grant(GrantAmount(5), entry_id=_eid(2), at=_LATER)  # 6
    acct.charge(_RUN_A, entry_id=_eid(3), at=_LATER)  # 5
    acct.charge(_RUN_B, entry_id=_eid(4), at=_LATER)  # 4
    acct.refund(_RUN_A, entry_id=_eid(5), at=_LATER)  # 5
    assert acct.balance == 5


def test_notice_for_a_welcome_account_is_first_credit() -> None:
    assert _open(OpeningGrant.WELCOME).notice == CreditNotice.FIRST_CREDIT


def test_notice_for_an_existing_account_is_credits_introduced() -> None:
    assert _open(OpeningGrant.EXISTING_ACCOUNT).notice == CreditNotice.CREDITS_INTRODUCED


@pytest.mark.parametrize("opening", list(OpeningGrant))
def test_notice_is_none_once_anything_was_charged(opening: OpeningGrant) -> None:
    acct = _open(opening)
    acct.charge(_RUN_A, entry_id=_eid(2), at=_LATER)
    assert acct.notice is None


def test_notice_stays_none_after_a_refund_of_the_only_charge() -> None:
    acct = _open()
    acct.charge(_RUN_A, entry_id=_eid(2), at=_LATER)
    acct.refund(_RUN_A, entry_id=_eid(3), at=_LATER)
    assert acct.notice is None


def test_a_grant_alone_does_not_clear_the_notice() -> None:
    acct = _open()
    acct.grant(GrantAmount(3), entry_id=_eid(2), at=_LATER)
    assert acct.notice == CreditNotice.FIRST_CREDIT


# ---- Charge: CR-2, CR-3 --------------------------------------------------------------------------


def test_a_charge_appends_minus_one_and_records_credit_charged() -> None:
    acct = _open()
    acct.release_events()
    acct.charge(_RUN_A, entry_id=_eid(2), at=_LATER)
    assert acct.balance == 0
    last = acct.entries[-1]
    assert (last.kind, last.credits, last.run, last.id, last.at) == (
        CreditEntryKind.CHARGE,
        -1,
        _RUN_A,
        _eid(2),
        _LATER,
    )
    assert acct.release_events() == (
        CreditCharged(occurred_at=_LATER, entry_id=_eid(2), run=_RUN_A),
    )


def test_a_charge_at_zero_is_no_credits_left_and_adds_nothing() -> None:
    acct = _open()
    acct.charge(_RUN_A, entry_id=_eid(2), at=_LATER)
    acct.release_events()
    with pytest.raises(NoCreditsLeft):
        acct.charge(_RUN_B, entry_id=_eid(3), at=_LATER)
    assert acct.balance == 0
    assert len(acct.entries) == 2
    assert acct.release_events() == ()


def test_charging_the_same_run_twice_is_run_already_charged() -> None:
    acct = _open()
    acct.grant(GrantAmount(5), entry_id=_eid(2), at=_LATER)
    acct.charge(_RUN_A, entry_id=_eid(3), at=_LATER)
    before = len(acct.entries)
    with pytest.raises(RunAlreadyCharged) as info:
        acct.charge(_RUN_A, entry_id=_eid(4), at=_LATER)
    assert info.value.run == _RUN_A
    assert len(acct.entries) == before
    assert acct.balance == 5


def test_a_second_charge_of_a_run_at_zero_balance_is_still_run_already_charged() -> None:
    # Both CR-2 and CR-3 apply; the idempotency answer is the more useful one (the run IS charged).
    acct = _open()
    acct.charge(_RUN_A, entry_id=_eid(2), at=_LATER)
    with pytest.raises(RunAlreadyCharged):
        acct.charge(_RUN_A, entry_id=_eid(3), at=_LATER)


def test_a_charge_refuses_a_bad_instant_and_adds_nothing() -> None:
    acct = _open()
    with pytest.raises(InvariantViolated):
        acct.charge(_RUN_A, entry_id=_eid(2), at=datetime(2026, 11, 7, 12, 0, 0))
    assert len(acct.entries) == 1


# ---- Refund: CR-4, CR-5 --------------------------------------------------------------------------


def test_refunding_a_charged_run_returns_true_and_gives_the_credit_back() -> None:
    acct = _open()
    acct.charge(_RUN_A, entry_id=_eid(2), at=_LATER)
    acct.release_events()
    assert acct.refund(_RUN_A, entry_id=_eid(3), at=_LATER) is True
    assert acct.balance == 1
    last = acct.entries[-1]
    assert (last.kind, last.credits, last.run) == (CreditEntryKind.REFUND, 1, _RUN_A)
    assert acct.release_events() == (
        CreditRefunded(occurred_at=_LATER, entry_id=_eid(3), run=_RUN_A),
    )


def test_refunding_a_run_never_charged_returns_false_and_records_nothing() -> None:
    acct = _open()
    acct.release_events()
    assert acct.refund(_RUN_A, entry_id=_eid(2), at=_LATER) is False
    assert len(acct.entries) == 1
    assert acct.balance == 1
    assert acct.release_events() == ()


def test_a_second_refund_of_the_same_run_returns_false_and_records_nothing() -> None:
    acct = _open()
    acct.charge(_RUN_A, entry_id=_eid(2), at=_LATER)
    assert acct.refund(_RUN_A, entry_id=_eid(3), at=_LATER) is True
    acct.release_events()
    assert acct.refund(_RUN_A, entry_id=_eid(4), at=_LATER) is False
    assert acct.balance == 1
    assert len(acct.entries) == 3
    assert acct.release_events() == ()


def test_a_refund_only_matches_its_own_run() -> None:
    acct = _open()
    acct.charge(_RUN_A, entry_id=_eid(2), at=_LATER)
    assert acct.refund(_RUN_B, entry_id=_eid(3), at=_LATER) is False
    assert acct.balance == 0


# ---- Grant ---------------------------------------------------------------------------------------


def test_a_grant_appends_the_amount_and_records_credits_granted() -> None:
    acct = _open()
    acct.release_events()
    acct.grant(GrantAmount(7), entry_id=_eid(2), at=_LATER)
    assert acct.balance == 8
    last = acct.entries[-1]
    assert (last.kind, last.credits, last.run) == (CreditEntryKind.GRANT, 7, None)
    assert acct.release_events() == (
        CreditsGranted(occurred_at=_LATER, entry_id=_eid(2), credits=7),
    )


def test_a_grant_refuses_a_bad_instant() -> None:
    acct = _open()
    with pytest.raises(InvariantViolated):
        acct.grant(
            GrantAmount(1),
            entry_id=_eid(2),
            at=datetime(2026, 11, 7, 12, 0, 0, 1, tzinfo=UTC),
        )
    assert len(acct.entries) == 1


# ---- CR-6: append-only ---------------------------------------------------------------------------


def test_entries_is_a_tuple_so_the_ledger_cannot_be_edited_through_it() -> None:
    acct = _open()
    assert isinstance(acct.entries, tuple)


def test_an_account_exposes_no_way_to_remove_or_replace_an_entry() -> None:
    public = {n for n in dir(CreditAccount) if not n.startswith("_")}
    assert not {
        n for n in public if n.startswith(("remove", "delete", "replace", "edit", "update"))
    }


def test_an_entry_cannot_be_reassigned() -> None:
    e = CreditEntry.charge(id=_eid(1), run=_RUN_A, at=_AT)
    with pytest.raises(AttributeError):
        e.credits = 5  # type: ignore[misc]


# ---- Pack click ----------------------------------------------------------------------------------


def test_the_first_pack_click_sets_the_instant_and_returns_true() -> None:
    acct = _open()
    acct.release_events()
    assert acct.first_pack_click_at is None
    assert acct.record_pack_click(Pack.PACK_3, at=_LATER) is True
    assert acct.first_pack_click_at == _LATER
    assert acct.release_events() == (
        PackClicked(occurred_at=_LATER, pack=Pack.PACK_3, first_click=True),
    )


def test_a_later_click_returns_false_keeps_the_first_instant_and_still_records() -> None:
    acct = _open()
    acct.record_pack_click(Pack.PACK_3, at=_LATER)
    acct.release_events()
    later = _LATER + timedelta(hours=1)
    assert acct.record_pack_click(Pack.PACK_10, at=later) is False
    assert acct.first_pack_click_at == _LATER
    assert acct.release_events() == (
        PackClicked(occurred_at=later, pack=Pack.PACK_10, first_click=False),
    )


def test_a_pack_click_changes_no_balance_and_adds_no_entry() -> None:
    acct = _open()
    acct.record_pack_click(Pack.PACK_10, at=_LATER)
    assert acct.balance == 1
    assert len(acct.entries) == 1


def test_a_pack_click_refuses_a_bad_instant() -> None:
    acct = _open()
    with pytest.raises(InvariantViolated):
        acct.record_pack_click(Pack.PACK_3, at=datetime(2026, 11, 7, 12, 0, 0))
    assert acct.first_pack_click_at is None


# ---- Payment notice opt-in -----------------------------------------------------------------------


def test_opting_in_sets_the_instant_and_records_the_choice() -> None:
    acct = _open()
    acct.release_events()
    assert acct.payment_notice_opted_in_at is None
    acct.choose_payment_notice(True, at=_LATER)
    assert acct.payment_notice_opted_in_at == _LATER
    assert acct.release_events() == (PaymentNoticeChosen(occurred_at=_LATER, opted_in=True),)


def test_opting_in_again_changes_nothing_and_records_nothing() -> None:
    acct = _open()
    acct.choose_payment_notice(True, at=_LATER)
    acct.release_events()
    acct.choose_payment_notice(True, at=_LATER + timedelta(hours=1))
    assert acct.payment_notice_opted_in_at == _LATER
    assert acct.release_events() == ()


def test_opting_out_clears_the_instant_and_records_the_choice() -> None:
    acct = _open()
    acct.choose_payment_notice(True, at=_LATER)
    acct.release_events()
    out = _LATER + timedelta(hours=1)
    acct.choose_payment_notice(False, at=out)
    assert acct.payment_notice_opted_in_at is None
    assert acct.release_events() == (PaymentNoticeChosen(occurred_at=out, opted_in=False),)


def test_opting_out_when_never_in_records_nothing() -> None:
    acct = _open()
    acct.release_events()
    acct.choose_payment_notice(False, at=_LATER)
    assert acct.payment_notice_opted_in_at is None
    assert acct.release_events() == ()


# ---- Pending entries -----------------------------------------------------------------------------


def test_pending_entries_are_those_appended_since_the_last_save() -> None:
    acct = _open()
    assert [e.kind for e in acct.pending_entries()] == [CreditEntryKind.WELCOME]
    acct.mark_entries_saved()
    assert acct.pending_entries() == ()
    acct.charge(_RUN_A, entry_id=_eid(2), at=_LATER)
    acct.grant(GrantAmount(2), entry_id=_eid(3), at=_LATER)
    assert [e.kind for e in acct.pending_entries()] == [
        CreditEntryKind.CHARGE,
        CreditEntryKind.GRANT,
    ]
    acct.mark_entries_saved()
    assert acct.pending_entries() == ()
    assert len(acct.entries) == 3


def test_a_refused_charge_leaves_nothing_pending() -> None:
    acct = _open()
    acct.mark_entries_saved()
    acct.charge(_RUN_A, entry_id=_eid(2), at=_LATER)
    acct.mark_entries_saved()
    with pytest.raises(NoCreditsLeft):
        acct.charge(_RUN_B, entry_id=_eid(3), at=_LATER)
    assert acct.pending_entries() == ()
