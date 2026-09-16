"""`SqlAlchemyTailoringRunRepository` — the `TailoringRunRepository` port (ADR-0007).

Filters below query `TailoringRun._id` / `TailoringRun._guest_session_id` / `TailoringRun._status`,
the **private** attributes the imperative mapping in
`infrastructure/persistence/mapping/tailoring/tailoring_run.py` targets — never `TailoringRun.id` /
`TailoringRun.guest_session_id` / `TailoringRun.status`. Those short names are plain read-only
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
from typing import TYPE_CHECKING, cast

import structlog
from sqlalchemy import func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.orm.exc import StaleDataError

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.tailoring.errors import (
    TailoringRunConcurrentlyModified,
    TailoringRunNotFound,
)
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunId, TailoringRunStatus
from tailorcraft.infrastructure.identifiers import uuid7
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
_TAILORING_RUN_GUEST_SESSION_ID: InstrumentedAttribute[GuestSessionId] = cast(
    "InstrumentedAttribute[GuestSessionId]", TailoringRun._guest_session_id
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
# client's poller keeps polling through, and `find_active_for_session` is the only reader.
_ACTIVE_STATUSES = (TailoringRunStatus.QUEUED, TailoringRunStatus.RUNNING)


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
        self._session.add(run)
        # `flush()`, not `commit()`: the transaction boundary belongs to the caller (a request or a
        # task), not to the repository.
        await self._session.flush()

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

    async def count_for_session(self, sid: GuestSessionId) -> int:
        """`SELECT count(*)`, not `len(await list_for_session(sid))` — the port docstring is explicit
        that this must not materialize every row just to measure them, and the saving is larger here
        than for the earlier two caps: each row carries a tailored CV *and* a cover letter, so
        counting by materializing would pull every document body of a session's whole history into
        memory to produce one integer."""
        result = await self._session.execute(
            select(func.count())
            .select_from(TailoringRun)
            .where(_TAILORING_RUN_GUEST_SESSION_ID == sid)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
        )
        return result.scalar_one()

    async def find_active_for_session(self, sid: GuestSessionId) -> TailoringRun | None:
        """The session's one run in flight — `queued` or `running` — or `None`.

        Returns the run rather than a bool because the 409 body carries its id, so a client that
        double-clicked can attach to the run already in flight instead of paying for a second call.

        `LIMIT 1` over an ordered query rather than `scalar_one_or_none()`, because the
        at-most-one-active rule is **soft** by decision (ADR-0014 §4): it spans aggregates, lives in
        `RequestTailoringRun`, and two genuinely concurrent requests may both pass it. That is
        accepted — but it means two active rows are possible, and `scalar_one_or_none()` would turn
        that accepted race into a `MultipleResultsFound` at the one moment the user is already
        confused. Ordering matches `list_for_session` so "the active one" means the newest, which is
        the run a double-clicking client wants to attach to.

        **No partial index on `(guest_session_id) WHERE status IN ('queued','running')`, and the
        absence is a decision rather than an oversight** (the same note sits on the column in the
        mapping module): with at most twenty runs per session, `ix_tailoring_run_guest_session_id`
        plus a filter on a handful of rows is free. A partial index is the change to make if a
        session ever holds thousands of runs — not before.
        """
        result = await self._session.execute(
            select(TailoringRun)
            .where(_TAILORING_RUN_GUEST_SESSION_ID == sid)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
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
