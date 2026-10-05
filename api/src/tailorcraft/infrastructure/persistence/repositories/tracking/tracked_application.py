"""`SqlAlchemyTrackedApplicationRepository` — the `TrackedApplicationRepository` port (ADR-0007,
ADR-0029, slice 3.1).

The shape of `SqlAlchemyTailoringRunRepository`, deliberately: `add` inside a SAVEPOINT with a
cross-table `FOR KEY SHARE` after the `INSERT`, `save` translating `StaleDataError`, constraint
refusals recognised by **name** through `database.violated_constraint` and never by message (the
message is withheld on purpose, `persistence/database.py`).

Filters use the mapped `Table`'s columns (`tracked_application_table.c.…`) or the `cast`
`InstrumentedAttribute`s below — never `TrackedApplication.id` / `.user_id`, which are plain
`@property`s: `select(...).where(TrackedApplication.id == x)` would evaluate a Python `==` and build
`WHERE true` or `WHERE false`, silently (`repositories/tailoring/tailoring_run.py`'s module note).

**The lock order, and why `KEY SHARE` on the run comes after the `INSERT`** (plan §0.7, AC-17…AC-19).
`tracking_application.tailoring_run_id` has no foreign key (ADR-0014/0016/0023: contexts' tables are
not fused), so nothing in the schema stops a card landing on a run a history deletion is removing.
2.3's two-lock pattern closes that race, and this module copies it rather than inventing one:

1. **The `INSERT` first.** Its FK check on `user_id` takes the `identity_user` row `FOR KEY SHARE`.
   Account erasure holds that row `FOR UPDATE` while it collects, so a card racing an erasure waits
   here and is then refused on `fk_tracking_application_user_id_identity_user` → `UserNotFound`
   (AC-19). Its unique index makes a concurrent second track of the same run wait for the first and
   then refuse on `uq_tracking_application_tailoring_run_id` → `ApplicationAlreadyTracked` carrying
   the winner's id.
2. **Then the run, `SELECT 1 FROM tailoring_run WHERE id = :r AND user_id = :u FOR KEY SHARE`.**
   - *Track first:* a history deletion's run `DELETE` conflicts with our `KEY SHARE` and waits until
     we commit; its card `DELETE` is a **separate statement**, so under READ COMMITTED it takes a
     fresh snapshot after the wait and sees our committed card. No card outlives its run.
   - *Deletion first:* our `SELECT … FOR KEY SHARE` waits on the deletion's row lock, then finds the
     row gone and returns nothing; the SAVEPOINT is rolled back (the card goes with it) and
     `TailoringRunNotFound` is raised — the 404 the use case's own authorization would have given a
     moment later (T-17).

   Locking the run **before** the `INSERT` would invert the order against erasure — erasure holds
   the user row and then cascades through runs, while we would hold the run and wait on the user
   row — which is a lock cycle. The user row is always first, everywhere (CLAUDE.md's
   `NOT EXISTS`/two-locks footgun, 2.3's `/verify`).

The refusal is **unconditional**: there is no constructor flag to skip it (2.3's lesson — a safety
check whose default is off protects only the callers that remember it). Test seeds insert cards over
runs that exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.orm.util import identity_key

from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.tailoring.errors import TailoringRunNotFound
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.domain.tracking.errors import (
    ApplicationAlreadyTracked,
    TrackedApplicationConcurrentlyModified,
    TrackedApplicationNotFound,
)
from tailorcraft.domain.tracking.tracked_application import TrackedApplication
from tailorcraft.domain.tracking.value_objects import TrackedApplicationId, TrackedRunRef
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.mapping.tracking.tracked_application import (
    tracked_application_table,
)

if TYPE_CHECKING:
    from tailorcraft.domain.tracking.ports import TrackedApplicationRepository

log = structlog.get_logger(__name__)

_table = tracked_application_table

# `TrackedApplication._id` is a class-body annotation only; `map_imperatively` installs the real
# `InstrumentedAttribute`. mypy sees the domain type, so the `cast` says what is there at runtime
# (`repositories/tailoring/tailoring_run.py` carries the full note).
_CARD_ID: InstrumentedAttribute[TrackedApplicationId] = cast(
    "InstrumentedAttribute[TrackedApplicationId]", TrackedApplication._id
)
_CARD_USER_ID: InstrumentedAttribute[UserId] = cast(
    "InstrumentedAttribute[UserId]", TrackedApplication._user_id
)
_CARD_RUN: InstrumentedAttribute[TrackedRunRef] = cast(
    "InstrumentedAttribute[TrackedRunRef]", TrackedApplication._tailoring_run_id
)

# Recognised by name, never by message (`violated_constraint`). Renaming either in
# `mapping/tracking/tracked_application.py` or the migration is a breaking change to `add` below.
_USER_FK: Final = "fk_tracking_application_user_id_identity_user"
_ONE_CARD_PER_RUN: Final = "uq_tracking_application_tailoring_run_id"


class SqlAlchemyTrackedApplicationRepository:
    """Persistence for `TrackedApplication`, backed by `tracking_application`.

    Hides its `AsyncSession` completely. The unit of work is the caller's: `flush`, never `commit`
    (the request handler commits, ADR-0007).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def next_identity(self) -> TrackedApplicationId:
        """Synchronous: application-assigned UUIDv7 needs no I/O (ADR-0007)."""
        return TrackedApplicationId(uuid7())

    async def add(self, card: TrackedApplication) -> None:
        """Insert `card`, then take its run `FOR KEY SHARE` — the module docstring has the lock order.

        **Inside a SAVEPOINT**, so a refused flush (or the run check's refusal) rolls back only this
        pending card and expires only it, not every instance the session holds (1.4's failed-flush
        lesson). `begin_nested()` flushes on entry; the caller (`TrackApplication`) holds no dirty
        aggregate at this point, and a card is never `add`ed twice, so that entry flush writes
        nothing past a version check (2.1's `save_rotation` footgun).

        Three refusals, nothing inserted for any of them:

        - the run is gone (or is not this user's) → `TailoringRunNotFound`;
        - `uq_tracking_application_tailoring_run_id` → `ApplicationAlreadyTracked(winner's id)`;
        - `fk_tracking_application_user_id_identity_user` → `UserNotFound`.

        Any other refusal propagates untranslated: an unrecognised constraint is never mistranslated.
        """
        # Read before the flush: after a nested rollback the pending card is expunged and expired.
        card_id = card.id
        user_id = card.user_id
        run = card.tailoring_run_id
        try:
            async with self._session.begin_nested():
                self._session.add(card)
                await self._session.flush()
                run_still_exists = (
                    await self._session.execute(
                        select(tailoring_run_table.c.id)
                        .where(
                            # The seam between the two contexts' id types, in the one adapter
                            # allowed to know both (`TailoringRunIdType` binds a `TailoringRunId`).
                            tailoring_run_table.c.id == TailoringRunId(run.value),
                            tailoring_run_table.c.user_id == user_id,
                        )
                        .with_for_update(key_share=True)
                    )
                ).scalar_one_or_none()
                if run_still_exists is None:
                    # Raised inside the SAVEPOINT so its rollback discards the card just flushed.
                    # Ids only in the message, as `SqlAlchemyTailoringRunRepository.get` does.
                    raise TailoringRunNotFound(f"no TailoringRun with id {run.value} for this user")
        except IntegrityError as exc:
            constraint = violated_constraint(exc)
            if constraint == _USER_FK:
                # `from None`: the listener already reduced the chain to identifiers, and the
                # frame holds `card` (its title is user text — Constitution §8).
                raise UserNotFound(f"the owner of {card_id!r} no longer exists") from None
            if constraint == _ONE_CARD_PER_RUN:
                winner = await self._id_for_run(run)
                if winner is not None:
                    raise ApplicationAlreadyTracked(winner) from None
                # The winner committed, refused us, and was then removed (an untrack or a history
                # deletion) before this read. Nothing honest to name: propagate the refusal, which
                # the router reports as the generic database failure, rather than invent an id.
                log.warning(
                    "tracking.already_tracked_winner_gone",
                    tracked_application_id=str(card_id.value),
                    tailoring_run_id=str(run.value),
                )
            raise

    async def _id_for_run(self, run: TrackedRunRef) -> TrackedApplicationId | None:
        """The id of the card for `run`, read in a fresh statement after the refused `INSERT`.

        Not owner-scoped, on purpose: a run has exactly one owner, and the use case authorized that
        owner before `add`, so the card holding the run's unique slot is necessarily the requester's.
        """
        return (
            await self._session.execute(select(_table.c.id).where(_table.c.tailoring_run_id == run))
        ).scalar_one_or_none()

    async def get(self, card_id: TrackedApplicationId) -> TrackedApplication:
        """By id, **without** an ownership check (the port's rule — authorization is the use case's).

        `from None` on the raise: nothing should chain here, and the use case's
        `TrackedApplicationNotFound from TrackedApplicationNotOwnedByUser` relies on a not-found from
        this method carrying no cause of its own.
        """
        found = (
            await self._session.execute(
                select(TrackedApplication).where(_CARD_ID == card_id)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
            )
        ).scalar_one_or_none()
        if found is None:
            raise TrackedApplicationNotFound(f"no TrackedApplication with id {card_id!r}") from None
        return found

    async def find_for_run(self, user_id: UserId, run: TrackedRunRef) -> TrackedApplication | None:
        """`user_id`'s card for `run`, or `None`. Seeks `uq_tracking_application_tailoring_run_id`;
        the user predicate keeps the answer the port's even if a run id were ever reused."""
        return (
            await self._session.execute(
                select(TrackedApplication).where(
                    _CARD_USER_ID == user_id,  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
                    _CARD_RUN == run,  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
                )
            )
        ).scalar_one_or_none()

    async def count_for_user(self, user_id: UserId) -> int:
        """`COUNT(*)` over the leading column of `ix_tracking_application_user_id_stage_changed_at`
        — at most the per-user cap's entries, no card materialized."""
        return (
            await self._session.execute(
                select(func.count()).select_from(_table).where(_table.c.user_id == user_id)
            )
        ).scalar_one()

    async def save(self, card: TrackedApplication) -> None:
        """Flush the card's current state; `StaleDataError` → `TrackedApplicationConcurrentlyModified`.

        The mapping declares `version` as `version_id_col` with `version_id_generator=False`, so the
        flush's `UPDATE … WHERE id = :id AND version = :loaded` matches no row when another writer
        moved the card on **or deleted it** since the load, and SQLAlchemy raises `StaleDataError`
        (AC-20). Nothing outside this module sees that vendor type.

        **The id is read BEFORE the flush, and that line is load-bearing** (1.4's footgun): a failed
        flush rolls the transaction back on the way out and expires every instance the session holds,
        `card` included; reading `card.id` in the `except` would be a lazy load on an inactive
        session — `PendingRollbackError`, a 503 where the contract says 409. The session is left in
        the failed state; rolling back is the caller's boundary, as committing is.
        """
        card_id = card.id
        self._session.add(card)
        try:
            await self._session.flush()
        except StaleDataError as exc:
            # The type only, never the message; `from None` keeps the frame (holding the card and
            # its user-written title) out of any report of the domain error.
            log.warning(
                "tracking.concurrent_modification",
                tracked_application_id=str(card_id.value),
                error_type=type(exc).__name__,
            )
            raise TrackedApplicationConcurrentlyModified(
                f"TrackedApplication {card_id.value} changed or was removed since it was loaded"
            ) from None

    async def remove(self, card_id: TrackedApplicationId) -> bool:
        """Core `DELETE … WHERE id = :id`; `True` iff a row went (`rowcount > 0`).

        **Any instance of this id leaves the identity map first.** The use case loaded it through
        `get` (and recorded `untrack` on it), so it is attached; a Core `DELETE` does not tell the ORM
        the row went, and a later flush of that instance — the caller's commit, or a
        `begin_nested()` entry flush — would issue its own `UPDATE` against a row that no longer
        exists (`SqlAlchemyBaseCvRepository.remove`'s reason).
        """
        stale = self._session.identity_map.get(identity_key(TrackedApplication, card_id))
        if stale is not None:
            self._session.expunge(stale)
        connection = await self._session.connection()
        result = await connection.execute(delete(_table).where(_table.c.id == card_id))
        return result.rowcount > 0


if TYPE_CHECKING:
    # Makes mypy prove the class structurally satisfies the port rather than trusting the shape by
    # eye. Never executed.
    def _assert_implements_tracked_application_repository(
        repo: SqlAlchemyTrackedApplicationRepository,
    ) -> None:
        _: TrackedApplicationRepository = repo
