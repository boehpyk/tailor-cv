"""Ports the `tracking` context needs from the outside world, in the domain's own language.

Two protocols: `TrackedApplicationRepository` (the write side, one aggregate at a time) and
`ApplicationBoardQuery` (the read side, every card at once). The repository is implemented in
`infrastructure/persistence/repositories/tracking/tracked_application.py`, the query in
`infrastructure/persistence/queries/application_board.py`. Neither module is imported here, and the
dependency only ever points this way.

**No sibling context is imported here either** (ADR-0029 decision 1; AC-6's allow-list walks this
module too). Two of `add`'s refusals are other contexts' errors — `TailoringRunNotFound` and
`UserNotFound` — and they are *named* in its docstring, never imported: the adapter, which may import
anything, raises them, and `application/tracking/` is the layer that catches them. The run a card
refers to arrives as tracking's own `TrackedRunRef`.

**These get no red-first cycle**, for the reason `domain/tailoring/ports.py` states: a `Protocol`
has no behaviour, so there is nothing to fail an assertion. What proves them is that the adapters
satisfy them under `mypy --strict` and the use cases compile against them.
"""

from __future__ import annotations

from typing import Protocol

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.tracking.board import ApplicationBoard
from tailorcraft.domain.tracking.tracked_application import TrackedApplication
from tailorcraft.domain.tracking.value_objects import TrackedApplicationId, TrackedRunRef


class TrackedApplicationRepository(Protocol):
    """Persistence for the `TrackedApplication` aggregate.

    The shape of `TailoringRunRepository` (`domain/tailoring/ports.py`), including its
    `get`-raises / `find`-returns-`None` asymmetry: `get` is called with an id the caller was handed
    and expects to exist, so absence is exceptional; `find_for_run` asks an ordinary question ("is
    this run tracked yet?") whose "no" is the common answer.

    No method here filters by owner except the two whose question is *about* an owner
    (`find_for_run`, `count_for_user`). Authorization is the use case's, as everywhere.
    """

    def next_identity(self) -> TrackedApplicationId:
        """Mint an id for a card that does not exist yet.

        Synchronous: identity is application-assigned (UUIDv7, ADR-0007) and needs no I/O, which is
        what lets `TrackedApplication.track` build a valid aggregate before it meets the database.
        """
        ...

    async def add(self, card: TrackedApplication) -> None:
        """Insert a card that does not exist yet.

        Three refusals, each a race the use case's own checks cannot close, because each is a
        concurrent writer acting *after* the check:

        - `TailoringRunNotFound` (tailoring's error, raised by the adapter) — the card's run was
          deleted concurrently, by a history-entry deletion (plan §0.7). There is no foreign key to
          the run (ADR-0029: contexts' tables are not fused), so the adapter serializes against that
          deletion with 2.3's two-lock pattern: insert, **then** take the run `FOR KEY SHARE` and
          refuse if it is gone. Either the insert wins and the deletion takes the card with the run,
          or the deletion wins and the insert is refused. Unconditional — no adapter may offer a way
          to skip it.
        - `ApplicationAlreadyTracked(existing_id)` — another request tracked the same run first (one
          card per run, a unique index). The use case's `find_for_run` check catches the ordinary
          repeat; this catches the double-click that slipped between that check and the insert, and
          carries the **winner's** id so the client can show the card it already has.
        - `UserNotFound` (identity's error, raised by the adapter) — the user was erased
          concurrently. Account erasure holds the user row while it collects, so no card lands after
          it.

        Nothing is inserted when any of them is raised. Says nothing about transactions: when a
        successful insert becomes durable is the caller's boundary.
        """
        ...

    async def get(self, card_id: TrackedApplicationId) -> TrackedApplication:
        """Raises `TrackedApplicationNotFound` if no card with this id exists.

        Does **not** check ownership. That is `TrackedApplicationNotOwnedByUser`, a use-case
        decision collapsed into the same 404 there — a repository that silently filtered by user
        would make the authorization rule invisible at the call site, and invisible rules are the
        ones a second entry point forgets.
        """
        ...

    async def find_for_run(self, user_id: UserId, run: TrackedRunRef) -> TrackedApplication | None:
        """`user_id`'s card for `run`, or `None` when the run is not tracked — an ordinary answer,
        never an error. `TrackApplication` asks it before `add`, so a repeat is answered with the
        existing card's id rather than with a refused insert."""
        ...

    async def count_for_user(self, user_id: UserId) -> int:
        """How many cards `user_id` has, for the `TooManyTrackedApplications` check. The cap is the
        use case's; this answers only the count, with `COUNT(*)` rather than by loading cards. Soft:
        two simultaneous tracks may both read one under the cap, and that is accepted."""
        ...

    async def save(self, card: TrackedApplication) -> None:
        """Persist the current state of a card this repository already handed out.

        Raises `TrackedApplicationConcurrentlyModified` when the row changed or vanished since this
        aggregate was loaded (its `version` no longer matches — ADR-0015 §3's mechanism). The
        aggregate cannot raise it: it sees only its own `version`, never the row's, which is why
        `TrackedApplicationVersionConflict` (the aggregate's own compare) is a different type. The
        adapter translates whatever its library throws; nothing outside it ever sees that.

        Says nothing about transactions, exactly as `add` does not.
        """
        ...

    async def remove(self, card_id: TrackedApplicationId) -> bool:
        """Delete the card with this id; `True` if a row went, `False` if there was none.

        A bool, not an exception, because "it was already gone" is a real outcome of a race the
        caller has already authorized past (a history deletion took the card between the use case's
        `get` and this call), and only the caller knows what it means: `UntrackApplication` turns
        `False` into `TrackedApplicationNotFound` and discards the events it recorded. Like
        `delete_session` (ADR-0018 (a)), the answer *is* the information — an adapter that returned
        nothing would make a deletion that did nothing indistinguishable from one that did.
        """
        ...


class ApplicationBoardQuery(Protocol):
    """A signed-in user's whole board, every card at once (slice 3.1, ADR-0024 and its amendment
    (a)).

    **A read-side port, not a repository.** It returns an `ApplicationBoard` of read-model cards and
    no aggregate: a board card joins a tracked application to its run, the run's posting and the
    run's base CV — four contexts' tables — and loading four aggregates per card to draw a board is
    the cost 2.2's T30 measured. It **never selects a document body**, and of a posting's text only
    a short preview: a card says which application it is and where it stands, and the documents are
    one click away on the run's own endpoint.

    **Unpaginated, deliberately** (ADR-0024 amendment (a)): a Kanban needs every column at once, and
    the board is bounded at write time by `TooManyTrackedApplications`' cap and measured at that
    bound. If the cap rises, this signature is the thing to revisit.
    """

    async def board_for_user(self, user_id: UserId) -> ApplicationBoard:
        """Every card `user_id` has, most recently moved first, ties broken by id.

        Scoped to the user by construction — the query takes no other owner — and the owner is in
        every join's condition, so a card is never lent another user's posting or CV. A card whose
        run, posting or base CV is gone is still returned, with that part `None`: a card the board
        hid could never be removed. An empty board for a user with no cards — never an error.
        """
        ...
