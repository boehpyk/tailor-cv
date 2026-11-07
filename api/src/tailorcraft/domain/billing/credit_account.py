"""The `CreditAccount` aggregate and its `CreditEntry` entities: an append-only ledger (ADR-0042).

The balance is derived from the entries, never stored beside them, so "never below zero" and "a refund
needs a charge" are checks the aggregate makes in memory. The database adds the concurrency half: two
partial unique indexes and one row lock.

**Skeleton (T5):** real signatures, no behaviour.
"""

from __future__ import annotations

from datetime import datetime

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
from tailorcraft.domain.shared.events import RecordsEvents


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
        raise NotImplementedError

    @classmethod
    def granted(cls, *, id: CreditEntryId, amount: GrantAmount, at: datetime) -> CreditEntry:
        raise NotImplementedError

    @classmethod
    def charge(cls, *, id: CreditEntryId, run: ChargedRunRef, at: datetime) -> CreditEntry:
        raise NotImplementedError

    @classmethod
    def refund(cls, *, id: CreditEntryId, run: ChargedRunRef, at: datetime) -> CreditEntry:
        raise NotImplementedError

    @property
    def id(self) -> CreditEntryId:
        raise NotImplementedError

    @property
    def kind(self) -> CreditEntryKind:
        raise NotImplementedError

    @property
    def credits(self) -> int:
        """Signed: -1 for a charge."""
        raise NotImplementedError

    @property
    def run(self) -> ChargedRunRef | None:
        raise NotImplementedError

    @property
    def at(self) -> datetime:
        raise NotImplementedError


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
        raise NotImplementedError

    @property
    def user_id(self) -> UserId:
        raise NotImplementedError

    @property
    def entries(self) -> tuple[CreditEntry, ...]:
        """Oldest first."""
        raise NotImplementedError

    @property
    def balance(self) -> int:
        """The sum of every entry's credits."""
        raise NotImplementedError

    @property
    def notice(self) -> CreditNotice | None:
        """`None` once anything was charged; before that, the opening kind decides which."""
        raise NotImplementedError

    @property
    def first_pack_click_at(self) -> datetime | None:
        raise NotImplementedError

    @property
    def payment_notice_opted_in_at(self) -> datetime | None:
        raise NotImplementedError

    def charge(self, run: ChargedRunRef, *, entry_id: CreditEntryId, at: datetime) -> None:
        """Appends -1 and records `CreditCharged`. `NoCreditsLeft` (CR-2), `RunAlreadyCharged` (CR-3)."""
        raise NotImplementedError

    def refund(self, run: ChargedRunRef, *, entry_id: CreditEntryId, at: datetime) -> bool:
        """Appends +1 and records `CreditRefunded`, returning `True`; `False` and nothing recorded
        when `run` was never charged (CR-5) or is already refunded (CR-4)."""
        raise NotImplementedError

    def grant(self, amount: GrantAmount, *, entry_id: CreditEntryId, at: datetime) -> None:
        """Appends `amount` and records `CreditsGranted`."""
        raise NotImplementedError

    def record_pack_click(self, pack: Pack, *, at: datetime) -> bool:
        """Sets `first_pack_click_at` on the first click ever and returns whether this was it; records
        `PackClicked` every time."""
        raise NotImplementedError

    def choose_payment_notice(self, opted_in: bool, *, at: datetime) -> None:
        """Sets or clears `payment_notice_opted_in_at`; records `PaymentNoticeChosen` only on a change."""
        raise NotImplementedError

    def pending_entries(self) -> tuple[CreditEntry, ...]:
        """The entries appended since the last `mark_entries_saved`: what `save` inserts."""
        raise NotImplementedError

    def mark_entries_saved(self) -> None:
        """Called by the repository after the flush; empties `pending_entries`."""
        raise NotImplementedError
