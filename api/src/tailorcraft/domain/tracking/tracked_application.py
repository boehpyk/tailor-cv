"""The `TrackedApplication` aggregate: one card on a user's board, for one succeeded run they sent.

Composes `RecordsEvents` and shares **no** base class with `TailoringRun`, although both carry a
`version` and an `expected_version` guard (plan §0.6). Their rules differ — a run is revisable only
when `succeeded`, a card always — and a base class would have to guess which rule it enforces. Two
short guards, each pointing at the other, is the house rule (CLAUDE.md).
"""

from __future__ import annotations

from datetime import datetime

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.domain.shared.events import RecordsEvents
from tailorcraft.domain.tracking.errors import TrackedApplicationVersionConflict
from tailorcraft.domain.tracking.events import (
    ApplicationStageChanged,
    ApplicationTracked,
    ApplicationUntracked,
)
from tailorcraft.domain.tracking.value_objects import (
    ApplicationStage,
    ApplicationTitle,
    TrackedApplicationId,
    TrackedRunRef,
)


# NOT `slots=True`: this aggregate is mapped by SQLAlchemy's *imperative* mapping
# (`registry.map_imperatively`), which instruments attributes on the instance `__dict__` — a
# `__slots__` class has none, so mapping would fail at wiring time rather than at import time. This
# is a deliberate departure from the value objects one module over, which are all `slots=True`
# because nothing maps them directly. `TailoringRun`, `BaseCv` and `JobPosting` carry the same
# comment, and the repetition is on purpose: a reader lands on one class, not on all four. The
# private-attribute names below are exactly what
# `infrastructure/persistence/mapping/tracking/tracked_application.py` targets, so renaming one here
# is a breaking change to that module too (ADR-0007).
class TrackedApplication(RecordsEvents):
    """One application a user is tracking: which run (the documents they sent), where they believe it
    stands, an optional title, and when it was tracked and last moved.

    **Invariants.**

    - **TA-1** Exactly one user and one run, both fixed by `track` and immutable after it — there is
      no setter for either.
    - **TA-2** The stage is always one of `ApplicationStage`'s six, and **any stage may move to any
      other** (paragraph below).
    - **TA-3** `tracked_at <= stage_changed_at`, and `stage_changed_at` never runs backwards: a move
      whose `at` is earlier than the current `stage_changed_at` is `InvariantViolated`. Every
      timestamp is timezone-aware and whole-second (the `Clock` contract, ADR-0007); a naive or
      sub-second `at` is `InvariantViolated`.
    - **TA-4** `version` is 1 at `track` and increases by exactly 1 per *effective* change — a move
      to another stage or a different title — and never on a no-op. The version check comes
      **before** the no-op check (OQ-11), so a stale tab is told it is stale even when the stage it
      wants already holds.
    - **TA-5** The title, when present, is an `ApplicationTitle` — never a bare `str`.

    **Why this is not `TailoringRun`'s table.** A reader who has just studied `TailoringRun` will
    find a strict transition table there — `queued → running → succeeded | failed`, terminal states
    refusing everything — and may want to "fix" this class to match. Don't. Each of a run's states
    records money spent and a call that happened, so a transition that did not happen must be
    unrepresentable. A card's stage records **what the user believes** about their job search, and
    belief is corrected: a misclick, an offer withdrawn, a "rejected" company that calls back. A
    strict machine (`applied → interviewing` only, `rejected` terminal) would invent rules the user's
    life does not follow and turn every correction into a support request. So `move_to` is legal
    from every stage to every other, and the invariants live where this aggregate's real risks are:
    **time** (TA-3) and **concurrency** (TA-4) — not order (ADR-0029 decision 3, plan §0.4).

    **A `UserId`, not an `Owner`** (plan §0.5). Every owned aggregate since 2.2 holds the sum type
    `GuestOwner | UserOwner` (ADR-0022). A card cannot be a guest's — the use case refuses one and
    there is no guest column — so holding `Owner` would be a type with an impossible variant.
    Authorization stays one equality: `card.user_id == requester`.

    **No `slots=True`** — see the comment above the class.
    """

    # Class-level annotations only: `__init__` sets nothing, so this is how `mypy --strict` learns
    # the attribute types `track` assigns and the properties read. Eight mapped attributes;
    # `_recorded_events` is not one of them (an in-memory outbox, not a persisted fact).
    _id: TrackedApplicationId
    # Never `Owner` — see the class docstring's "A `UserId`, not an `Owner`" paragraph.
    _user_id: UserId
    _tailoring_run_id: TrackedRunRef
    _stage: ApplicationStage
    _title: ApplicationTitle | None
    _tracked_at: datetime
    _stage_changed_at: datetime
    # The optimistic-concurrency counter the mapping declares as `version_id_col` with
    # `version_id_generator=False` (TA-4): the aggregate bumps it, the database refuses a write whose
    # loaded value no longer matches.
    _version: int

    def __init__(self) -> None:
        """Takes nothing and does nothing. Build a `TrackedApplication` with `track`.

        **Keep it, even though it looks deletable.** `registry.map_imperatively` installs a default
        constructor on a mapped class that defines none, and that constructor accepts the mapped
        attribute names — so without this, `TrackedApplication(_stage=ApplicationStage.OFFER)` would
        be a second way to build a card, skipping `track`, its version, its timestamps and its event
        (AC-3). A no-argument `__init__` makes any argument a `TypeError`. It must not `raise`: a
        mapped class is built through `cls()`, whose instrumentation wrapper attaches
        `_sa_instance_state`. `TailoringRun.__init__` carries the full account.
        """

    @classmethod
    def track(
        cls,
        *,
        id: TrackedApplicationId,
        user_id: UserId,
        tailoring_run_id: TrackedRunRef,
        stage: ApplicationStage,
        title: ApplicationTitle | None,
        at: datetime,
    ) -> TrackedApplication:
        """The only constructor: `version == 1`, `tracked_at == stage_changed_at == at`, and exactly
        one `ApplicationTracked` recorded (AC-3). A naive or sub-second `at` is `InvariantViolated`
        (TA-3).

        Named `track` because that is what the user does — they *track* an application. "Only a
        succeeded, user-owned run" is checked by the use case before this is called: the aggregate
        cannot see a run (`domain/tracking` imports no sibling context).
        """
        _require_clock_instant(at)
        card = cls()
        card._id = id
        card._user_id = user_id
        card._tailoring_run_id = tailoring_run_id
        card._stage = stage
        card._title = title
        card._tracked_at = at
        card._stage_changed_at = at
        card._version = 1
        card.record(
            ApplicationTracked(
                occurred_at=at,
                tracked_application_id=id,
                user_id=user_id,
                tailoring_run_id=tailoring_run_id,
                stage=stage,
            )
        )
        return card

    def move_to(self, stage: ApplicationStage, *, expected_version: int, at: datetime) -> None:
        """Move the card to `stage` — from any stage (TA-2).

        Order: `expected_version != version` → `TrackedApplicationVersionConflict` (before the no-op
        rule, TA-4); same stage → no-op (nothing changes, no event); `at` earlier than
        `stage_changed_at`, naive or sub-second → `InvariantViolated` (TA-3); otherwise set the
        stage, `stage_changed_at = at`, `version += 1`, record `ApplicationStageChanged` (AC-4).

        The version guard is written here and again in `TailoringRun._guard_revisable`, on purpose:
        same shape, different rules around it (plan §0.6).
        """
        self._guard_version(expected_version)
        if stage is self._stage:
            return
        _require_clock_instant(at)
        if at < self._stage_changed_at:
            raise InvariantViolated(
                "a tracked application's stage cannot change before it last did"
            )

        previous = self._stage
        self._stage = stage
        self._stage_changed_at = at
        self._version += 1
        self.record(
            ApplicationStageChanged(
                occurred_at=at,
                tracked_application_id=self._id,
                user_id=self._user_id,
                from_stage=previous,
                to_stage=stage,
            )
        )

    def retitle(
        self, title: ApplicationTitle | None, *, expected_version: int, at: datetime
    ) -> None:
        """Set or clear the title (AC-5).

        Version check first, as `move_to`; the same title → no-op; otherwise set it and `version +=
        1`. **Records no event** — an event would have to carry the title, which no event may — and
        **does not touch `stage_changed_at`**: a renamed card has not moved.
        """
        self._guard_version(expected_version)
        if title == self._title:
            return
        # `at` is validated though nothing stores it: the method's contract is the same as
        # `move_to`'s, and a caller handing a naive instant here would hand one there too.
        _require_clock_instant(at)

        self._title = title
        self._version += 1

    def untrack(self, at: datetime) -> None:
        """Record `ApplicationUntracked` (AC-6). Deleting the row is the repository's job; the use
        case publishes the event only if that deletion removed something."""
        _require_clock_instant(at)
        self.record(
            ApplicationUntracked(
                occurred_at=at, tracked_application_id=self._id, user_id=self._user_id
            )
        )

    def _guard_version(self, expected_version: int) -> None:
        # The same compare `TailoringRun._guard_revisable` makes, written twice on purpose: the rules
        # around it differ (a run is revisable only when `succeeded`, a card always), so a shared
        # helper would have to guess which one it enforces (plan §0.6).
        if expected_version != self._version:
            raise TrackedApplicationVersionConflict(
                expected_version=expected_version, current_version=self._version
            )

    @property
    def id(self) -> TrackedApplicationId:
        return self._id

    @property
    def user_id(self) -> UserId:
        """The user this card belongs to — a `UserId`, never an `Owner` (class docstring)."""
        return self._user_id

    @property
    def tailoring_run_id(self) -> TrackedRunRef:
        return self._tailoring_run_id

    @property
    def stage(self) -> ApplicationStage:
        return self._stage

    @property
    def title(self) -> ApplicationTitle | None:
        return self._title

    @property
    def tracked_at(self) -> datetime:
        return self._tracked_at

    @property
    def stage_changed_at(self) -> datetime:
        return self._stage_changed_at

    @property
    def version(self) -> int:
        return self._version


def _require_clock_instant(at: datetime) -> None:
    """TA-3: every instant a card holds is timezone-aware and whole-second (the `Clock` contract,
    ADR-0007), so a database round trip can never change it."""
    # `utcoffset() is None` rather than `tzinfo is None`: a tzinfo may still decline an offset, and
    # such a datetime is naive for every comparison that matters (`HistoryCursor`'s reason).
    if at.utcoffset() is None:
        raise InvariantViolated("a tracked application's instants must be timezone-aware")
    if at.microsecond != 0:
        raise InvariantViolated("a tracked application's instants must be whole-second")
