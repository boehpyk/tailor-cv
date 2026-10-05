"""The `TrackedApplication` aggregate: one card on a user's board, for one succeeded run they sent.

Composes `RecordsEvents` and shares **no** base class with `TailoringRun`, although both carry a
`version` and an `expected_version` guard (plan §0.6). Their rules differ — a run is revisable only
when `succeeded`, a card always — and a base class would have to guess which rule it enforces. Two
short guards, each pointing at the other, is the house rule (CLAUDE.md).

This module is at its **skeleton** step (T3): real signatures, real attribute names (the imperative
mapping at T14 targets them), and `NotImplementedError` bodies, so that `qa`'s T4 tests fail on their
assertions rather than on an `ImportError`. T5 fills the bodies in.
"""

from __future__ import annotations

from datetime import datetime

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.events import RecordsEvents
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
        raise NotImplementedError

    def move_to(self, stage: ApplicationStage, *, expected_version: int, at: datetime) -> None:
        """Move the card to `stage` — from any stage (TA-2).

        Order: `expected_version != version` → `TrackedApplicationVersionConflict` (before the no-op
        rule, TA-4); same stage → no-op (nothing changes, no event); `at` earlier than
        `stage_changed_at`, naive or sub-second → `InvariantViolated` (TA-3); otherwise set the
        stage, `stage_changed_at = at`, `version += 1`, record `ApplicationStageChanged` (AC-4).

        The version guard is written here and again in `TailoringRun._guard_revisable`, on purpose:
        same shape, different rules around it (plan §0.6).
        """
        raise NotImplementedError

    def retitle(
        self, title: ApplicationTitle | None, *, expected_version: int, at: datetime
    ) -> None:
        """Set or clear the title (AC-5).

        Version check first, as `move_to`; the same title → no-op; otherwise set it and `version +=
        1`. **Records no event** — an event would have to carry the title, which no event may — and
        **does not touch `stage_changed_at`**: a renamed card has not moved.
        """
        raise NotImplementedError

    def untrack(self, at: datetime) -> None:
        """Record `ApplicationUntracked` (AC-6). Deleting the row is the repository's job; the use
        case publishes the event only if that deletion removed something."""
        raise NotImplementedError

    @property
    def id(self) -> TrackedApplicationId:
        raise NotImplementedError

    @property
    def user_id(self) -> UserId:
        """The user this card belongs to — a `UserId`, never an `Owner` (class docstring)."""
        raise NotImplementedError

    @property
    def tailoring_run_id(self) -> TrackedRunRef:
        raise NotImplementedError

    @property
    def stage(self) -> ApplicationStage:
        raise NotImplementedError

    @property
    def title(self) -> ApplicationTitle | None:
        raise NotImplementedError

    @property
    def tracked_at(self) -> datetime:
        raise NotImplementedError

    @property
    def stage_changed_at(self) -> datetime:
        raise NotImplementedError

    @property
    def version(self) -> int:
        raise NotImplementedError
