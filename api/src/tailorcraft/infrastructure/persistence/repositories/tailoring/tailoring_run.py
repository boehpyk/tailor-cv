"""`SqlAlchemyTailoringRunRepository` — the `TailoringRunRepository` port (ADR-0007).

Filters below query `TailoringRun._id` / `TailoringRun._owner_guest_session_id` / `TailoringRun._status`,
the **private** attributes the imperative mapping in
`infrastructure/persistence/mapping/tailoring/tailoring_run.py` targets — never `TailoringRun.id` /
`TailoringRun.owner` / `TailoringRun.status`. Those short names are plain read-only
`@property` objects on the domain class, not `InstrumentedAttribute`s:
`select(TailoringRun).where(TailoringRun.id == x)` would call the property, get back a
`TailoringRunId`, evaluate a bare Python `==` against `x`, and build `select(...).where(True)` or
`.where(False)` — a predicate that matches everything or nothing, silently, with no error anywhere.
It looks like a typo the first time you see it; it is not one. See the identical note in
`repositories/intake/base_cv.py` and `repositories/posting/job_posting.py`.

This is the codebase's third repository and the first that is **written by one process and read by
another** — the API creates a run, a Celery worker loads it, mutates it and persists it again — so it
is also the first to implement `save` and `find`. Both carry their reason at the method.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Final, assert_never, cast

import structlog
from sqlalchemy import ColumnElement, func, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.orm.exc import StaleDataError

from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.posting.errors import JobPostingNotFound
from tailorcraft.domain.tailoring.errors import (
    TailoringRunConcurrentlyModified,
    TailoringRunNotFound,
)
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunId, TailoringRunStatus
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.types.tailoring import TailoringRunStatusType

if TYPE_CHECKING:
    from tailorcraft.domain.tailoring.ports import TailoringRunRepository

log = structlog.get_logger(__name__)

# `TailoringRun._id` etc. are class-body annotations only (`domain/tailoring/tailoring_run.py`
# assigns no value — `map_imperatively`'s `properties=` installs the real `InstrumentedAttribute` at
# import time), so mypy sees them typed as the *domain* type (`TailoringRunId`,
# `TailoringRunStatus`, `datetime`, …) rather than as SQLAlchemy's mapped attribute.
# `TailoringRun._id == run_id` then type-checks as `TailoringRunId.__eq__` returning a plain `bool` —
# which is exactly the runtime trap the module docstring describes, just caught by mypy instead of by
# a silently-empty query. These `cast`s tell mypy what is actually there at runtime without touching
# behaviour; each is a `cast`, not an `Any`, so CLAUDE.md's ban on unjustified `Any` does not apply.
_TAILORING_RUN_ID: InstrumentedAttribute[TailoringRunId] = cast(
    "InstrumentedAttribute[TailoringRunId]", TailoringRun._id
)
_TAILORING_RUN_GUEST_SESSION_ID: InstrumentedAttribute[GuestSessionId | None] = cast(
    "InstrumentedAttribute[GuestSessionId | None]", TailoringRun._owner_guest_session_id
)
_TAILORING_RUN_USER_ID: InstrumentedAttribute[UserId | None] = cast(
    "InstrumentedAttribute[UserId | None]", TailoringRun._owner_user_id
)
_TAILORING_RUN_STATUS: InstrumentedAttribute[TailoringRunStatus] = cast(
    "InstrumentedAttribute[TailoringRunStatus]", TailoringRun._status
)
_TAILORING_RUN_REQUESTED_AT: InstrumentedAttribute[datetime] = cast(
    "InstrumentedAttribute[datetime]", TailoringRun._requested_at
)
_TAILORING_RUN_STARTED_AT: InstrumentedAttribute[datetime | None] = cast(
    "InstrumentedAttribute[datetime | None]", TailoringRun._started_at
)

# The two non-terminal statuses, named once. `TailoringRunStatus` documents them as the two a
# client's poller keeps polling through; `find_active_for_session` and `find_active_for_owner` are
# the only readers.
_ACTIVE_STATUSES = (TailoringRunStatus.QUEUED, TailoringRunStatus.RUNNING)

# Recognised by name, never by message (`violated_constraint`). Renaming the FK in
# `mapping/tailoring/tailoring_run.py` is a breaking change to `add` below.
_USER_FK: Final = "fk_tailoring_run_user_id_identity_user"


def _owned_by(owner: Owner) -> ColumnElement[bool]:
    """The owner as a `WHERE` clause: the variant picks the column, and
    `ck_tailoring_run_exactly_one_owner` guarantees the other is empty on any row that matches."""
    match owner:
        case GuestOwner(guest_session_id=guest_session_id):
            return _TAILORING_RUN_GUEST_SESSION_ID == guest_session_id  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
        case UserOwner(user_id=user_id):
            return _TAILORING_RUN_USER_ID == user_id  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
        case _:
            assert_never(owner)


class SqlAlchemyTailoringRunRepository:
    """Persistence for `TailoringRun`, backed by `tailoring_run`.

    Hides its `AsyncSession` completely — nothing outside this module ever sees it (ADR-0007). The
    unit of work (commit/rollback) is the caller's concern: one transaction per request in
    `infrastructure/api/deps.py::get_session`, and **two** per execution in the worker, because
    `ExecuteTailoringRun` has to make `running` visible to a polling client while a twelve-second
    call is still in flight (the port's `save` docstring).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def next_identity(self) -> TailoringRunId:
        """Synchronous: application-assigned UUIDv7 needs no I/O (ADR-0007). Here it does a second
        job — the id minted at request time is the only argument the queued task ever receives."""
        return TailoringRunId(uuid7())

    async def add(self, run: TailoringRun) -> None:
        """Insert `run`; raise `UserNotFound` if its `UserOwner` no longer exists (H-53), and
        `JobPostingNotFound` if its posting no longer does.

        `flush()`, not `commit()`: the transaction boundary belongs to the caller (a request or a
        task), not to the repository.

        The race and the shape are `SqlAlchemyBaseCvRepository.add`'s (2.2's S-12): an account
        erasure holding the `identity_user` row `FOR UPDATE` makes this `INSERT`'s FK check wait,
        then refuse on `fk_tailoring_run_user_id_identity_user` once the erasure commits — "the user
        is gone", the same error and the same 401 as `resolve_existing_user`. It is also what makes
        erasure complete (AC-37): no run can land after the erasure collected its keys.

        **Inside a SAVEPOINT**, so a refused flush expires only this pending run and not every
        instance in the session (the 1.4 lesson). Any other refusal propagates untranslated.

        **Then the run's posting is locked `FOR KEY SHARE`, and a vanished posting is
        `JobPostingNotFound`** (2.3 /verify, reviewer MINOR #1). A cross-table lock in a repository
        is unusual, so the reason: `tailoring_run.job_posting_id` has no FK (ADR-0014 declined the
        cross-context one), and a history-entry deletion removes a user's posting with a
        `DELETE … WHERE NOT EXISTS (a run on it)`. Nothing serialized the two, so a run authorized
        against posting P could race P's deletion **in either direction** and land referencing a
        posting that no longer exists:

        - *insert first* — this `INSERT` is uncommitted when the deletion's `NOT EXISTS` runs, so
          it cannot see the row and deletes P. Now the deletion takes P `FOR UPDATE`, which waits
          behind this `FOR KEY SHARE` until our transaction ends, and then re-reads in a fresh
          statement that sees the committed run: P is kept.
        - *delete first* — the use case authorized P, then the deletion committed. This
          `SELECT … FOR KEY SHARE` finds no row (READ COMMITTED re-checks a row that was deleted
          while it waited, too), so the insert is refused `JobPostingNotFound` — the same 404 the
          authorization would have given a moment later — and the SAVEPOINT's rollback takes the
          run with it.

        **The refusal is unconditional — there is no switch to turn it off** (2.3 /verify round 2,
        reviewer MAJOR). A check that is off by default is off in the next composition root that
        forgets it, so every caller of `add` gets it: the request routes today, and 2.4's claim or
        any CLI tomorrow (the codebase's "strict default, no off switch" rule, ADR-0012).

        **The lock follows the `INSERT`, never precedes it**, and the order is load-bearing: the
        `INSERT`'s FK check takes the *owner* row `FOR KEY SHARE` first, exactly as before. An
        account erasure (user row `FOR UPDATE`, then postings) or a guest purge (session row, then
        its cascade) therefore meets us on the owner row before either of us touches the posting,
        so the two can queue but never deadlock. Locking the posting first would let us hold P
        while waiting on the user row the erasure holds while it waits on P.

        Guest runs take the same path; it is harmless there (a guest posting is deleted only by the
        purge's session cascade, which the owner-row ordering above already serializes).
        """
        # Read before the flush: after a nested rollback the pending run is expunged.
        run_id = run.id
        posting_id = run.job_posting_id
        try:
            async with self._session.begin_nested():
                self._session.add(run)
                await self._session.flush()
                posting_still_exists = (
                    await self._session.execute(
                        select(job_posting_table.c.id)
                        .where(job_posting_table.c.id == posting_id)
                        .with_for_update(key_share=True)
                    )
                ).scalar_one_or_none()
                if posting_still_exists is None:
                    # Raised inside the SAVEPOINT so its rollback discards the run just flushed.
                    # The message names the id only, as `SqlAlchemyJobPostingRepository.get` does.
                    raise JobPostingNotFound(f"no JobPosting with id {posting_id!r}")
        except IntegrityError as exc:
            if violated_constraint(exc) == _USER_FK:
                # `from None`: the listener already reduced the chain to identifiers, and the frame
                # holds `run` (Constitution §8, E-9).
                raise UserNotFound(f"the owner of {run_id!r} no longer exists") from None
            raise

    async def save(self, run: TailoringRun) -> None:
        """Make the run's current state the state the next read hands back.

        A run reached this repository through `get`/`find`, so it is already persistent in this
        session's identity map and the flush alone emits the `UPDATE`; `add()` is a no-op for such an
        instance and is kept only so a run that has become detached (a session closed between the
        load and the save) is re-attached rather than silently not written.

        `flush`, not `commit`, for the same reason as `add` — but note that the *caller's* two
        commits are the load-bearing part here, not this method (port docstring, technical-plan
        "Transaction boundaries"). `save` promises durability to the next read in this transaction;
        when that reaches disk is the caller's boundary.

        Raises `TailoringRunConcurrentlyModified` when the row moved on since this aggregate was
        loaded (port docstring; ADR-0015 §3, TR-8). The mapping declares `version` as
        `version_id_col`, so the flush's `UPDATE … WHERE id = :id AND version = :loaded` matches no
        row and SQLAlchemy raises `StaleDataError` — a vendor exception nothing outside this module
        may see, translated here into the domain's name for it. The session is left in the failed
        state a failed flush leaves it in: rolling back is the caller's boundary, exactly as
        committing is.

        **The id is read BEFORE the flush, and that line is load-bearing.** A failed flush rolls
        its transaction back on the way out, and that rollback `_expire`s every instance the
        session holds — `run` included, because it is the dirty one. The session's transaction is
        then inactive until the caller rolls back, so touching `run.id` inside the `except` below
        would try to re-load an expired attribute on a session that refuses to run a query, and the
        `StaleDataError` this branch exists to translate would surface instead as a
        `PendingRollbackError` — a `SQLAlchemyError` nothing above this layer maps, rendered as a
        503 for what is a 409. Found by slice 1.4's AC-12(b) API test, the first thing to drive
        this branch against a real database; a plain `TailoringRunId` value taken up front is
        immune to the expiry. "Every instance" is the root boundary's behaviour, which is what this
        bare repository gets in the API; the worker's `CommittingTailoringRunRepository` wraps this
        flush in a SAVEPOINT precisely so that the expiry stops at the dirty run (its docstring has
        the measurement).
        """
        run_id = run.id
        self._session.add(run)
        try:
            await self._session.flush()
        except StaleDataError as exc:
            # The type only, never the message: SQLAlchemy's message names the table and the
            # primary key, and `hide_parameters` is what keeps the bound values (the CV) out of the
            # driver's line — but the *frame* holds `run`, which holds up to four documents, and
            # `include_local_variables=False` is a setting rather than a law. `from None` makes the
            # frame unreachable from any report of the domain error (E-9; Constitution §8).
            log.warning(
                "tailoring.concurrent_modification",
                tailoring_run_id=str(run_id.value),
                error_type=type(exc).__name__,
            )
            raise TailoringRunConcurrentlyModified(run_id) from None

    async def get(self, run_id: TailoringRunId) -> TailoringRun:
        # The column stays on the left of `==` below (silencing ruff's SIM300 "Yoda condition"):
        # see the module-level cast comment for why swapping it to satisfy that check would
        # silently re-break mypy.
        result = await self._session.execute(
            select(TailoringRun).where(_TAILORING_RUN_ID == run_id)  # noqa: SIM300
        )
        found = result.scalar_one_or_none()
        if found is None:
            # **`from None` is load-bearing, not tidiness.** `tests/integration/tailoring/
            # test_read_tailoring_runs.py` asserts the *negative*: a `TailoringRunNotFound` raised
            # for a never-issued id must NOT chain from `TailoringRunNotOwnedBySession`, which is
            # what proves the API's shared 404 (G-29/AC-14) does not leak "that run exists, but it
            # is not yours". That assertion discriminates only because the raise here — and in
            # `FakeTailoringRunRepository.get` — suppresses the context. Let any exception chain
            # through and the test silently stops proving anything, and **nothing fails**.
            #
            # `scalar_one_or_none()` happens to raise nothing to chain from today; the `from None`
            # is on the `raise` rather than trusting that, because a future rewrite to
            # `scalar_one()` (which raises `NoResultFound`) would otherwise reintroduce the chain
            # with no visible failure anywhere.
            raise TailoringRunNotFound(f"no TailoringRun with id {run_id!r}") from None
        return found

    async def find(self, run_id: TailoringRunId) -> TailoringRun | None:
        """`None` where `get` raises — **this is the worker's lookup** (G-26).

        The asymmetry is deliberate and documented at the port: the task is handed an id that may
        legitimately have stopped existing between the enqueue and the pickup (a guest session
        purged at the 24-hour mark cascades its runs away while a message for one of them is still
        in Redis, and `task_acks_late=True` makes redelivery of an already-purged id real). Absence
        is an ordinary branch there — the task returns `MISSING`, logs a line and stops — where it is
        exceptional on the API's read path, which asked for one specific run.
        """
        result = await self._session.execute(
            select(TailoringRun).where(_TAILORING_RUN_ID == run_id)  # noqa: SIM300
        )
        return result.scalar_one_or_none()

    async def list_for_session(self, sid: GuestSessionId) -> Sequence[TailoringRun]:
        """Newest first, for `GET /api/tailoring-runs`.

        **The `id DESC` tiebreak is not decoration.** The `Clock` port is whole-second by contract
        and `requested_at` is stored at whole-second precision, so two runs requested inside the same
        second are ordinary rather than exotic — and with `requested_at DESC` alone their relative
        order is whatever the plan happens to produce, which is to say undefined and free to change
        when the table grows an index or a row. `TailoringRunId` is a UUIDv7, so its byte order is
        time order at a finer resolution than the column keeps: the tiebreak is not arbitrary, it is
        the same "newest first" one resolution down.
        """
        result = await self._session.execute(
            select(TailoringRun)
            .where(_TAILORING_RUN_GUEST_SESSION_ID == sid)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
            .order_by(_TAILORING_RUN_REQUESTED_AT.desc(), _TAILORING_RUN_ID.desc())
        )
        return result.scalars().all()

    async def count_for_owner(self, owner: Owner) -> int:
        """`SELECT count(*)` over the owner's column — `count_for_session`'s reason (no document
        body is materialized to produce one integer), either variant.

        A guest's count seeks `ix_tailoring_run_guest_session_id`; a user's, the leading column of
        `ix_tailoring_run_user_id_requested_at` — at most 500 index entries (the per-user cap).
        """
        result = await self._session.execute(
            select(func.count()).select_from(TailoringRun).where(_owned_by(owner))
        )
        return result.scalar_one()

    async def find_active_for_owner(self, owner: Owner) -> TailoringRun | None:
        """The owner's newest run in flight — `queued` or `running` — or `None`.

        `find_active_for_session`'s contract and shape, either variant: `LIMIT 1` over the newest-
        first order rather than `scalar_one_or_none()`, because the at-most-one-active rule is soft
        by decision (ADR-0014 §4) and two active rows are an accepted race, not an error. For a
        user the owner column's index plus a status filter reads at most 500 entries; the note on
        the missing partial index applies unchanged.
        """
        result = await self._session.execute(
            select(TailoringRun)
            .where(_owned_by(owner))
            .where(_TAILORING_RUN_STATUS.in_(_ACTIVE_STATUSES))
            .order_by(_TAILORING_RUN_REQUESTED_AT.desc(), _TAILORING_RUN_ID.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def list_stale_running(
        self, started_before: datetime, limit: int
    ) -> Sequence[TailoringRun]:
        """The contract is `TailoringRunRepository.list_stale_running`'s docstring: which runs, the
        `NULL` fold, the total order, the bound, no locking. It is not restated here, so there is one
        copy to keep true. `FakeTailoringRunRepository` implements the same contract. What follows
        is only what this adapter adds.

        **The status reaches Postgres as a literal (`literal_execute=True`), not a bound
        parameter.** `ix_tailoring_run_running_started_at` is a partial index, and the planner uses
        one only when it can prove the query's `WHERE` implies the index's. A generic prepared plan
        holding `status = $1` proves nothing, so on a table big enough to matter the sweep could
        fall back to a sequential scan with nothing reporting it. That was measured, not reasoned:
        `EXPLAIN (GENERIC_PLAN)` gave a Seq Scan for `status = $3` and an Index Scan for
        `status = 'running'`. The value is still rendered through `TailoringRunStatusType`, so the
        literal is the decorator's, not a second spelling of the enum.

        **`NULLS FIRST` is spelled out** because an ascending Postgres sort puts `NULL` *last*, the
        opposite of what the port promises.

        **The index serves the `WHERE`, not the `ORDER BY`.** It is on `started_at` alone, so
        Postgres sorts the handful of `running` rows it returns. The mapping module records why that
        is enough, and why the index is partial at all.
        """
        running = literal(
            TailoringRunStatus.RUNNING, TailoringRunStatusType(), literal_execute=True
        )
        result = await self._session.execute(
            select(TailoringRun)
            .where(_TAILORING_RUN_STATUS == running)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
            .where(
                or_(
                    _TAILORING_RUN_STARTED_AT < started_before,  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
                    _TAILORING_RUN_STARTED_AT.is_(None),
                )
            )
            .order_by(_TAILORING_RUN_STARTED_AT.asc().nulls_first(), _TAILORING_RUN_ID.asc())
            .limit(limit)
        )
        return result.scalars().all()


if TYPE_CHECKING:
    # Makes mypy prove `SqlAlchemyTailoringRunRepository` structurally satisfies
    # `TailoringRunRepository` rather than trusting the shape by eye. Never executed — a `Protocol`
    # needs no instance to check against, only a compatible signature — so it costs nothing at
    # runtime. This is also what `domain/tailoring/ports.py` points at when it explains why a
    # Protocol gets no red-first cycle of its own.
    def _assert_implements_tailoring_run_repository(
        repo: SqlAlchemyTailoringRunRepository,
    ) -> None:
        _: TailoringRunRepository = repo
