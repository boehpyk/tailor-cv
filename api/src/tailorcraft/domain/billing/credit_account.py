"""The `CreditAccount` aggregate and its `CreditEntry` entities: an append-only ledger (ADR-0042).

The balance is derived from the entries, never stored beside them, so "never below zero" and "a refund
needs a charge" are checks the aggregate makes in memory. The database adds the concurrency half: two
partial unique indexes and one row lock.

"""

from __future__ import annotations

from datetime import datetime

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
from tailorcraft.domain.shared.events import RecordsEvents


def _require_whole_second(at: datetime) -> None:
    """CE-3 and the account's instants: aware and whole-second, else `InvariantViolated`."""
    # `utcoffset() is None` rather than `tzinfo is None`: a tzinfo may still decline an offset.
    if at.utcoffset() is None:
        raise InvariantViolated("a billing instant must be timezone-aware")
    if at.microsecond != 0:
        raise InvariantViolated("a billing instant must be whole-second")


# NOT `slots=True`: imperative mapping instruments the instance `__dict__` (`TailoringRun`'s reason).
class CreditEntry:
    """One line of the history. Never changes once made: read-only properties, no mutator.

    **Invariants.**

    - **CE-1** `credits` matches `kind`: +1 opening, 1..100 grant, -1 charge, +1 refund.
    - **CE-2** `run` is set if and only if the kind is `charge` or `refund`.
    - **CE-3** `at` is timezone-aware and whole-second, else `InvariantViolated`.

    One named constructor per kind, so CE-1 and CE-2 hold by construction. The aggregate calls them.
    """

    _id: CreditEntryId
    _kind: CreditEntryKind
    _credits: int
    _run: ChargedRunRef | None
    _at: datetime

    def __init__(self) -> None:
        """Takes nothing and does nothing. Build an entry with a named constructor.

        **Keep it.** `registry.map_imperatively` installs a default constructor on a mapped class that
        defines none, and it accepts the mapped attribute names, which would skip every invariant. It
        must not raise: a mapped class is built through `cls()`.
        """

    @classmethod
    def opening(cls, *, id: CreditEntryId, grant: OpeningGrant, at: datetime) -> CreditEntry:
        return cls._make(id, CreditEntryKind(grant.value), 1, None, at)

    @classmethod
    def granted(cls, *, id: CreditEntryId, amount: GrantAmount, at: datetime) -> CreditEntry:
        return cls._make(id, CreditEntryKind.GRANT, amount.value, None, at)

    @classmethod
    def charge(cls, *, id: CreditEntryId, run: ChargedRunRef, at: datetime) -> CreditEntry:
        return cls._make(id, CreditEntryKind.CHARGE, -1, run, at)

    @classmethod
    def refund(cls, *, id: CreditEntryId, run: ChargedRunRef, at: datetime) -> CreditEntry:
        return cls._make(id, CreditEntryKind.REFUND, 1, run, at)

    @classmethod
    def _make(
        cls,
        id: CreditEntryId,
        kind: CreditEntryKind,
        credits: int,
        run: ChargedRunRef | None,
        at: datetime,
    ) -> CreditEntry:
        """The one writer, private, so CE-1 and CE-2 are whatever the four constructors above say."""
        _require_whole_second(at)
        entry = cls()
        entry._id = id
        entry._kind = kind
        entry._credits = credits
        entry._run = run
        entry._at = at
        return entry

    @property
    def id(self) -> CreditEntryId:
        return self._id

    @property
    def kind(self) -> CreditEntryKind:
        return self._kind

    @property
    def credits(self) -> int:
        """Signed: -1 for a charge."""
        return self._credits

    @property
    def run(self) -> ChargedRunRef | None:
        return self._run

    @property
    def at(self) -> datetime:
        return self._at


# NOT `slots=True`, for the same reason as `CreditEntry`.
class CreditAccount(RecordsEvents):
    """One registered user's credits.

    **Invariants.**

    - **CR-1** Exactly one opening entry, and it is the first.
    - **CR-2** The balance never goes below zero: a charge at 0 is `NoCreditsLeft`.
    - **CR-3** At most one charge per run: `RunAlreadyCharged`.
    - **CR-4** At most one refund per run: a second returns `False`.
    - **CR-5** A refund needs a charge: none returns `False`, recording nothing. A run that predates
      credits is ordinary, not an error.
    - **CR-6** Entries are never removed or replaced: there is no method that could.

    Every instant is timezone-aware and whole-second, else `InvariantViolated`. Entry ids are passed
    in (`CreditAccountRepository.next_entry_identity()`, ADR-0007), keeping the aggregate I/O-free.
    """

    _user_id: UserId
    _opened_at: datetime
    _entries: list[CreditEntry]
    _first_pack_click_at: datetime | None
    _payment_notice_opted_in_at: datetime | None
    # Not mapped. A loaded account never ran `open`, so this is read through `_pending_list`.
    _pending: list[CreditEntry]

    def __init__(self) -> None:
        """Takes nothing and does nothing. Build an account with `open`. See `CreditEntry.__init__`."""

    @classmethod
    def open(
        cls,
        *,
        user_id: UserId,
        opening: OpeningGrant,
        entry_id: CreditEntryId,
        at: datetime,
    ) -> CreditAccount:
        """One opening entry; records `CreditAccountOpened`."""
        entry = CreditEntry.opening(id=entry_id, grant=opening, at=at)  # checks `at` (CE-3)
        account = cls()
        account._user_id = user_id
        account._opened_at = at
        account._entries = []
        account._first_pack_click_at = None
        account._payment_notice_opted_in_at = None
        account._append(entry)  # CR-1: the first entry, and no other method makes an opening one
        account.record(CreditAccountOpened(occurred_at=at, opening=opening))
        return account

    @property
    def user_id(self) -> UserId:
        return self._user_id

    @property
    def entries(self) -> tuple[CreditEntry, ...]:
        """Oldest first. A tuple, so CR-6 cannot be broken through it."""
        return tuple(self._entries)

    @property
    def balance(self) -> int:
        """The sum of every entry's credits."""
        return sum(entry.credits for entry in self._entries)

    @property
    def notice(self) -> CreditNotice | None:
        """`None` once anything was charged, refunded or not; before that, the opening kind decides."""
        if self._has_entry(CreditEntryKind.CHARGE):
            return None
        match self._entries[0].kind:
            case CreditEntryKind.EXISTING_ACCOUNT:
                return CreditNotice.CREDITS_INTRODUCED
            case _:
                return CreditNotice.FIRST_CREDIT

    @property
    def first_pack_click_at(self) -> datetime | None:
        return self._first_pack_click_at

    @property
    def payment_notice_opted_in_at(self) -> datetime | None:
        return self._payment_notice_opted_in_at

    def charge(self, run: ChargedRunRef, *, entry_id: CreditEntryId, at: datetime) -> None:
        """Appends -1 and records `CreditCharged`. `NoCreditsLeft` (CR-2), `RunAlreadyCharged` (CR-3).

        CR-3 is checked first: when both apply, "this run is already charged" is the true answer for a
        retried request, and "no credits" would send the person to buy one they already spent.
        """
        if self._has_entry(CreditEntryKind.CHARGE, run):
            raise RunAlreadyCharged(run)
        if self.balance < 1:
            raise NoCreditsLeft("no credits left")
        self._append(CreditEntry.charge(id=entry_id, run=run, at=at))
        self.record(CreditCharged(occurred_at=at, entry_id=entry_id, run=run))

    def refund(self, run: ChargedRunRef, *, entry_id: CreditEntryId, at: datetime) -> bool:
        """Appends +1 and records `CreditRefunded`, returning `True`; `False` and nothing recorded
        when `run` was never charged (CR-5) or is already refunded (CR-4)."""
        if not self._has_entry(CreditEntryKind.CHARGE, run) or self._has_entry(
            CreditEntryKind.REFUND, run
        ):
            return False
        self._append(CreditEntry.refund(id=entry_id, run=run, at=at))
        self.record(CreditRefunded(occurred_at=at, entry_id=entry_id, run=run))
        return True

    def grant(self, amount: GrantAmount, *, entry_id: CreditEntryId, at: datetime) -> None:
        """Appends `amount` and records `CreditsGranted`."""
        self._append(CreditEntry.granted(id=entry_id, amount=amount, at=at))
        self.record(CreditsGranted(occurred_at=at, entry_id=entry_id, credits=amount.value))

    def record_pack_click(self, pack: Pack, *, at: datetime) -> bool:
        """Sets `first_pack_click_at` on the first click ever and returns whether this was it; records
        `PackClicked` every time."""
        _require_whole_second(at)
        first = self._first_pack_click_at is None
        if first:
            self._first_pack_click_at = at
        self.record(PackClicked(occurred_at=at, pack=pack, first_click=first))
        return first

    def choose_payment_notice(self, opted_in: bool, *, at: datetime) -> None:
        """Sets or clears `payment_notice_opted_in_at`; records `PaymentNoticeChosen` only on a change."""
        _require_whole_second(at)
        if opted_in == (self._payment_notice_opted_in_at is not None):
            return
        self._payment_notice_opted_in_at = at if opted_in else None
        self.record(PaymentNoticeChosen(occurred_at=at, opted_in=opted_in))

    def pending_entries(self) -> tuple[CreditEntry, ...]:
        """The entries appended since the last `mark_entries_saved`: what `save` inserts."""
        return tuple(self._pending_list())

    def mark_entries_saved(self) -> None:
        """Called by the repository after the flush; empties `pending_entries`."""
        self._pending = []

    def _append(self, entry: CreditEntry) -> None:
        """The only writer of `_entries`, and it only appends (CR-6)."""
        self._entries.append(entry)
        self._pending_list().append(entry)

    def _pending_list(self) -> list[CreditEntry]:
        # A loaded account was built without `open`, so it has no buffer yet: nothing is pending.
        if not hasattr(self, "_pending"):
            self._pending = []
        return self._pending

    def _has_entry(self, kind: CreditEntryKind, run: ChargedRunRef | None = None) -> bool:
        """Whether an entry of `kind` exists, for `run` when one is named."""
        return any(e.kind == kind and (run is None or e.run == run) for e in self._entries)
