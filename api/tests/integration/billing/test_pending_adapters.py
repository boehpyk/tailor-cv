"""What the in-memory fakes cannot show, now that the adapters exist (slice 5.2, T28; held in T14).

The fakes in `tests/integration/fakes.py` have no transaction and no lock, so "the refund and the count
vanish with the failure" and "the loser loses everything" can only be shown on the real repositories
and the real `AsyncSession`. Each held spec is written here from its docstring (the acceptance
criterion), not from the adapters.

**Where the held specs went.** AC-8/F-2 (two charges), AC-13/F-15 (a refund racing a charge), AC-6/AC-7
(commit together or neither) are HTTP-level, so they live in `tests/api/test_credits_races.py`; the
confirm-commits-together spec is `tests/api/test_registration_credits.py`. What stays is what needs the
worker's own composition root and no HTTP: AC-9, F-14, AC-27.

**How a fault is injected: below the repositories, on the connection.** A listener arms when the
counter's upsert has executed (the last write of a failure: mark, refund, **count**, save) and raises on
whatever the transaction does next, as the database going away would. The refund is by then already
written, so what the test proves is that it does not survive. Nothing is patched in a repository, so a
change to a repository cannot silently route around the fault. Every faulted test has a **control** that is
the same flow without the fault, so "nothing survived" cannot be an arrangement that never wrote anything.

**Arrangement is SQL and the repositories**, never the use case under test; rows are committed on the
test's session (a SAVEPOINT release), so the fault's rollback undoes exactly what the failing step wrote.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.application.billing.record_pack_click import (
    RecordPackClick,
    RecordPackClickCommand,
)
from tailorcraft.application.billing.refund_credit_for_run import RefundCreditForRun
from tailorcraft.application.tailoring.execute_tailoring_run import (
    ExecuteTailoringRunCommand,
    ExecuteTailoringRunOutcome,
)
from tailorcraft.application.tailoring.record_run_failure import RecordRunFailure
from tailorcraft.application.tailoring.record_run_not_queued import (
    RecordRunNotQueued,
    RecordRunNotQueuedCommand,
)
from tailorcraft.domain.billing.value_objects import Pack
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.tailoring.errors import LlmTimedOut
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.clock import SystemClock
from tailorcraft.infrastructure.events.logging_publisher import LoggingEventPublisher
from tailorcraft.infrastructure.persistence.demand.daily_counter import SqlAlchemyDailyCounter
from tailorcraft.infrastructure.persistence.repositories.billing.credit_account import (
    SqlAlchemyCreditAccountRepository,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container
from tests.integration.fakes import FakeLlm
from tests.integration.owners import (
    a_draft,
    extracted_cv,
    extraction_failed_cv,
    pasted_posting,
    queued_run,
    running_run,
)

_PHC = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"

SITES = ["llm_failure", "base_cv_deleted", "cv_text_missing", "abandoned_redelivery", "not_queued"]
_NOW = SystemClock


# ----------------------------------------------------------------------------------- arrangement


async def new_owner(session: AsyncSession | AsyncConnection) -> UserOwner:
    """A user, its account (opening +1) and nothing else, written as SQL. Does not commit."""
    user_id = uuid4()
    at = _NOW().now() - timedelta(hours=3)
    await session.execute(
        text(
            "INSERT INTO identity_user (id, email, password_hash, created_at, password_updated_at) "
            "VALUES (:i, :e, :p, :t, :t)"
        ),
        {"i": user_id, "e": f"t28-{user_id.hex}@example.com", "p": _PHC, "t": at},
    )
    await session.execute(
        text("INSERT INTO billing_credit_account (user_id, opened_at) VALUES (:i, :t)"),
        {"i": user_id, "t": at},
    )
    await session.execute(
        text(
            "INSERT INTO billing_credit_entry (id, user_id, kind, credits, tailoring_run_id, created_at) "
            "VALUES (:id, :u, 'welcome', 1, NULL, :t)"
        ),
        {"id": uuid4(), "u": user_id, "t": at},
    )
    return UserOwner(UserId(user_id))


async def charge(session: AsyncSession | AsyncConnection, owner: UserOwner, run_id: UUID) -> None:
    """The charge `RequestTailoringRun` would have written for this run."""
    await session.execute(
        text(
            "INSERT INTO billing_credit_entry (id, user_id, kind, credits, tailoring_run_id, created_at) "
            "VALUES (:id, :u, 'charge', -1, :r, :t)"
        ),
        {
            "id": uuid4(),
            "u": owner.user_id.value,
            "r": run_id,
            "t": _NOW().now() - timedelta(hours=2),
        },
    )


@dataclass
class Seeded:
    owner: UserOwner
    run_id: UUID
    original_status: str


async def seed_run(
    session: AsyncSession,
    owner: UserOwner,
    *,
    kind: str,
    cv: str = "extracted",
    age: timedelta = timedelta(minutes=10),
) -> Seeded:
    """One **charged** run in state `kind` ('queued' | 'running') over a posting (always) and a CV
    ('extracted' | 'failed' | 'absent': an absent CV is the `base_cv_deleted` state). Committed."""
    at = _NOW().now() - age
    cv_row = None
    if cv != "absent":
        cv_row = (extracted_cv if cv == "extracted" else extraction_failed_cv)(owner, at)
        await SqlAlchemyBaseCvRepository(session).add(cv_row)
    posting = pasted_posting(owner, at)
    await SqlAlchemyJobPostingRepository(session).add(posting)
    builder = running_run if kind == "running" else queued_run
    cv_id = cv_row.id if cv_row is not None else BaseCvId(uuid4())  # an absent CV: an id, no row
    run = builder(owner, at, job_posting_id=posting.id, base_cv_id=cv_id)
    run_id = run.id.value
    await SqlAlchemyTailoringRunRepository(session).add(run)
    await charge(session, owner, run_id)
    await session.commit()
    return Seeded(owner, run_id, kind)


async def scalar(session: AsyncSession | AsyncConnection, sql: str, **params: Any) -> Any:
    return (await session.execute(text(sql), params)).scalar_one()


async def counter(session: AsyncSession | AsyncConnection, name: str) -> int:
    return int(
        await scalar(
            session,
            "SELECT COALESCE(SUM(count), 0) FROM demand_daily_counter WHERE counter = :c",
            c=name,
        )
    )


async def run_status(session: AsyncSession | AsyncConnection, run_id: UUID) -> str:
    return str(await scalar(session, "SELECT status FROM tailoring_run WHERE id = :i", i=run_id))


async def refunds(session: AsyncSession | AsyncConnection, run_id: UUID) -> int:
    return int(
        await scalar(
            session,
            "SELECT count(*) FROM billing_credit_entry WHERE kind = 'refund' AND tailoring_run_id = :r",
            r=run_id,
        )
    )


@contextmanager
def a_database_failure_after_the_counter(engine: AsyncEngine) -> Iterator[list[bool]]:
    """Arms when the `demand_daily_counter` upsert has executed on any connection of `engine`; the
    **next** thing that transaction does (a statement, or its commit) raises, once, as the database
    going away would. `fired` records that it did.

    Why the counter, and why "whatever is next". `RecordRunFailure` writes *mark, refund, count, save*,
    and the ORM autoflushes the dirty run at the refund's first read, so the run's `UPDATE` is **not**
    the last write: the count is. After it only the save's SAVEPOINT bookkeeping and the commit remain,
    and which statement runs next depends on the caller (the worker's committing repository, the
    handler's teardown). The refund is already written by then.

    **Real, committed sessions, not the rolled-back `session` fixture.** That fixture emulates a commit
    as `RELEASE SAVEPOINT`, and SQLAlchemy does not roll back a savepoint whose release failed (it
    leaves that to the real connection's rollback, which the fixture never takes), so a failed commit
    would look like it had persisted. Against a real connection the fault and the rollback behave as
    they do in production. SAVEPOINT bookkeeping statements are never failed: that is the
    transaction's own, and failing one before it is emitted leaves the session half built."""
    armed: list[bool] = []
    fired: list[bool] = []

    def after(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.lstrip().startswith("INSERT INTO demand_daily_counter"):
            armed.append(True)

    def explode(*args: Any) -> None:
        if armed:
            armed.clear()  # once: the rollback that follows must be able to run
            fired.append(True)
            raise OperationalError("injected", {}, Exception("injected: the database went away"))

    def explode_on_a_statement(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if not statement.lstrip().startswith(("SAVEPOINT", "RELEASE", "ROLLBACK")):
            explode()

    sync = engine.sync_engine
    hooks = [
        ("after_cursor_execute", after),
        ("before_cursor_execute", explode_on_a_statement),
        ("commit", explode),
    ]
    for name, fn in hooks:
        event.listen(sync, name, fn)
    try:
        yield fired
    finally:
        for name, fn in hooks:
            event.remove(sync, name, fn)


def _llm(site: str) -> FakeLlm:
    if site == "llm_failure":
        return FakeLlm(LlmTimedOut())
    return FakeLlm(a_draft())


async def drive_site(
    site: str, session: AsyncSession, settings: Settings, owner: UserOwner
) -> tuple[UUID, Callable[[], Any]]:
    """Arrange one charged run for `site` and return its id with the coroutine function that drives
    the production path to its failure write."""
    if site == "not_queued":
        seeded = await seed_run(session, owner, kind="queued")
        runs = SqlAlchemyTailoringRunRepository(session)
        record = RecordRunNotQueued(
            runs,
            RecordRunFailure(
                runs,
                RefundCreditForRun(SqlAlchemyCreditAccountRepository(session)),
                SqlAlchemyDailyCounter(session),
                LoggingEventPublisher(),
            ),
            _NOW(),
        )

        async def go() -> None:
            await record(RecordRunNotQueuedCommand(tailoring_run_id=TailoringRunId(seeded.run_id)))
            await (
                session.commit()
            )  # the handler's teardown: the API repository never commits itself

        return seeded.run_id, go
    kinds = {
        "llm_failure": {"kind": "queued"},
        "base_cv_deleted": {"kind": "queued", "cv": "absent"},
        "cv_text_missing": {"kind": "queued", "cv": "failed"},
        "abandoned_redelivery": {"kind": "running", "age": timedelta(hours=1)},
    }[site]
    seeded = await seed_run(session, owner, **kinds)  # type: ignore[arg-type]
    execute = container._build_use_case(settings, session, _llm(site))
    return seeded.run_id, lambda: execute(
        ExecuteTailoringRunCommand(tailoring_run_id=TailoringRunId(seeded.run_id))
    )


# --------------------------------------------------------------------------- AC-9: five sites


@pytest_asyncio.fixture
async def committed_users(engine: AsyncEngine, settings: Settings) -> AsyncIterator[list[UUID]]:
    """Users a test committed; deleted at teardown (the cascades take their runs, CVs and ledger)."""
    assert "_test" in settings.database_url
    ids: list[UUID] = []
    yield ids
    async with engine.begin() as conn:
        for user_id in ids:
            await conn.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id})


async def committed_owner(engine: AsyncEngine, committed_users: list[UUID]) -> UserOwner:
    async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
        owner = await new_owner(seeding)
        await seeding.commit()
    committed_users.append(owner.user_id.value)
    return owner


async def facts(engine: AsyncEngine, run_id: UUID) -> tuple[str, int, int]:
    """(run status, refunds for it, `runs_failed` total), read on a fresh connection."""
    async with engine.connect() as conn:
        return (
            await run_status(conn, run_id),
            await refunds(conn, run_id),
            await counter(conn, "runs_failed"),
        )


@pytest.mark.parametrize("site", SITES)
async def test_a_fault_after_the_refund_write_leaves_neither_the_failure_nor_the_refund(
    site: str, engine: AsyncEngine, settings: Settings, committed_users: list[UUID]
) -> None:
    """AC-9. For each failure site reachable from the worker and the handler: a charged run, the real
    credit and run repositories and the real counter on one session; the database fails **after** the
    refund (and the count) is written; the caller rolls back. The run is **not** `failed`, **no**
    refund entry exists, and `runs_failed` did not move."""
    owner = await committed_owner(engine, committed_users)
    async with async_sessionmaker(engine, expire_on_commit=False)() as work:
        run_id, drive = await drive_site(site, work, settings, owner)
        status_before, _, failed_before = await facts(engine, run_id)

        with a_database_failure_after_the_counter(engine) as fired:
            with pytest.raises(OperationalError):
                await drive()
            await work.rollback()

    assert fired == [True], (
        "the fault must land after the refund was written, or this proves nothing"
    )
    status, refunded, failed = await facts(engine, run_id)
    # The worker commits `running` (step 4) before the outcome it then loses; the handler site has no
    # such step. Either way the run is undecided and uncredited.
    assert status in (status_before, "running")
    assert (refunded, failed) == (0, failed_before)


@pytest.mark.parametrize("site", SITES)
async def test_control_the_same_site_without_the_fault_fails_refunds_and_counts_together(
    site: str, engine: AsyncEngine, settings: Settings, committed_users: list[UUID]
) -> None:
    owner = await committed_owner(engine, committed_users)
    async with async_sessionmaker(engine, expire_on_commit=False)() as work:
        run_id, drive = await drive_site(site, work, settings, owner)
        _, _, failed_before = await facts(engine, run_id)

        await drive()

    status, refunded, failed = await facts(engine, run_id)
    assert (status, refunded, failed - failed_before) == ("failed", 1, 1)


async def seed_for_list(session: AsyncSession, owner: UserOwner, list_name: str) -> Seeded:
    if list_name == "stale_running":
        return await seed_run(session, owner, kind="running", age=timedelta(hours=1))
    return await seed_run(session, owner, kind="queued", age=timedelta(hours=2))


@pytest.mark.parametrize("list_name", ["stale_running", "lost_queued"])
async def test_a_fault_after_the_sweeps_refund_write_leaves_neither_failure_nor_refund(
    list_name: str, engine: AsyncEngine, settings: Settings, committed_users: list[UUID]
) -> None:
    """AC-9, the sweep's `abandoned` and `not_queued` sites, through the real
    `CommittingTailoringRunRepository`."""
    owner = await committed_owner(engine, committed_users)
    async with async_sessionmaker(engine, expire_on_commit=False)() as work:
        seeded = await seed_for_list(work, owner, list_name)
        sweep = container._build_sweep_use_case(settings, work)
        status_before, _, failed_before = await facts(engine, seeded.run_id)

        with a_database_failure_after_the_counter(engine) as fired:
            with pytest.raises(OperationalError):
                await sweep()
            await work.rollback()

    assert fired == [True]
    assert await facts(engine, seeded.run_id) == (status_before, 0, failed_before)


@pytest.mark.parametrize("list_name", ["stale_running", "lost_queued"])
async def test_control_the_sweep_without_the_fault_fails_refunds_and_counts_each_run_once(
    list_name: str, engine: AsyncEngine, settings: Settings, committed_users: list[UUID]
) -> None:
    owner = await committed_owner(engine, committed_users)
    async with async_sessionmaker(engine, expire_on_commit=False)() as work:
        seeded = await seed_for_list(work, owner, list_name)
        _, _, failed_before = await facts(engine, seeded.run_id)

        result = await container._build_sweep_use_case(settings, work)()

    assert (result.abandoned, result.lost_queued) == (
        (1, 0) if list_name == "stale_running" else (0, 1)
    )
    status, refunded, failed = await facts(engine, seeded.run_id)
    assert (status, refunded, failed - failed_before) == ("failed", 1, 1)


# ------------------------------------------------------------------------------------ F-14


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PRODUCTION DEFECT (qa, T28): the loser's version conflict surfaces as a raw StaleDataError from "
        "the ORM autoflush inside SqlAlchemyCreditAccountRepository.find (the refund's first read), "
        "because RecordRunFailure marks the run before it refunds; the repository's save never gets to "
        "translate it to TailoringRunConcurrentlyModified, so AbandonStaleTailoringRuns does not contain "
        "it and the whole sweep aborts. The fix's commit removes this marker."
    ),
)
async def test_the_loser_of_a_sweep_against_redelivery_race_loses_its_refund_and_count_too(
    engine: AsyncEngine,
    settings: Settings,
    committed_users: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-14. Two stale `running` runs, r1 older than r2, so the sweep takes r1 first. The sweep has
    **already read r1** and written its refund when a redelivered task fails the same run on another
    connection and commits (the staging seam is the sweep's own first credit read, which every build of
    the failure collaborator makes before its save). The sweep's save then loses the version check.

    Exactly one side commits `failed`; afterwards the account holds **one** refund for r1 and `runs_failed`
    moved by **two** (r1 once, r2 once), not three: the loser's whole transaction, refund and count
    included, rolled back. The sweep carries on to r2, which it re-reads by id after the rollback."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as seeding:
        owner = await new_owner(seeding)
        committed_users.append(owner.user_id.value)
        r1 = await seed_run(seeding, owner, kind="running", age=timedelta(hours=2))
        r2 = await seed_run(seeding, owner, kind="running", age=timedelta(hours=1))
    async with engine.connect() as conn:
        failed_before = await counter(conn, "runs_failed")

    fired: list[ExecuteTailoringRunOutcome] = []
    original_find = SqlAlchemyCreditAccountRepository.find

    async def _the_redelivery_wins_first(self: Any, user_id: Any) -> Any:
        if not fired:
            fired.append(ExecuteTailoringRunOutcome.SKIPPED)  # placeholder: replaced below
            async with factory() as redelivery_session:
                execute = container._build_use_case(
                    settings, redelivery_session, FakeLlm(a_draft())
                )
                outcome = await execute(
                    ExecuteTailoringRunCommand(tailoring_run_id=TailoringRunId(r1.run_id))
                )
            fired[0] = outcome
        return await original_find(self, user_id)

    monkeypatch.setattr(SqlAlchemyCreditAccountRepository, "find", _the_redelivery_wins_first)

    async with factory() as sweep_session:
        result = await asyncio.wait_for(
            container._build_sweep_use_case(settings, sweep_session)(), timeout=30
        )

    assert fired == [ExecuteTailoringRunOutcome.ABANDONED], "the redelivery must win r1 mid-sweep"
    assert (result.abandoned, result.conflicts) == (1, 1)
    async with engine.connect() as conn:
        for run in (r1, r2):
            assert await run_status(conn, run.run_id) == "failed"
            assert await refunds(conn, run.run_id) == 1, "one refund per run, never two"
        assert await counter(conn, "runs_failed") - failed_before == 2


# --------------------------------------------------------------------------------------- AC-27


@pytest.mark.parametrize("counter_name", ["runs_failed", "runs_succeeded"])
async def test_a_run_counter_rolled_back_with_its_fact_counts_nothing(
    counter_name: str, engine: AsyncEngine, settings: Settings, committed_users: list[UUID]
) -> None:
    """AC-27 for the two the worker owns: the count is written in the transaction of the outcome it
    counts, so when that transaction fails the count goes with it. `runs_succeeded` is written before the
    success save and `runs_failed` before the failure save; the fault lands after either."""
    owner = await committed_owner(engine, committed_users)
    llm = FakeLlm(LlmTimedOut()) if counter_name == "runs_failed" else FakeLlm(a_draft())
    async with async_sessionmaker(engine, expire_on_commit=False)() as work:
        seeded = await seed_run(work, owner, kind="queued")
        execute = container._build_use_case(settings, work, llm)
        async with engine.connect() as conn:
            before = await counter(conn, counter_name)

        with a_database_failure_after_the_counter(engine) as fired:
            with pytest.raises(OperationalError):
                await execute(
                    ExecuteTailoringRunCommand(tailoring_run_id=TailoringRunId(seeded.run_id))
                )
            await work.rollback()

    assert fired == [True]
    async with engine.connect() as conn:
        assert await counter(conn, counter_name) == before
        assert await run_status(conn, seeded.run_id) == "running", "step 4's commit stands"


async def test_control_a_success_counts_runs_succeeded_once_and_keeps_the_credit(
    engine: AsyncEngine, settings: Settings, committed_users: list[UUID]
) -> None:
    owner = await committed_owner(engine, committed_users)
    async with async_sessionmaker(engine, expire_on_commit=False)() as work:
        seeded = await seed_run(work, owner, kind="queued")
        execute = container._build_use_case(settings, work, FakeLlm(a_draft()))
        async with engine.connect() as conn:
            before = await counter(conn, "runs_succeeded")

        outcome = await execute(
            ExecuteTailoringRunCommand(tailoring_run_id=TailoringRunId(seeded.run_id))
        )

    assert outcome is ExecuteTailoringRunOutcome.SUCCEEDED
    async with engine.connect() as conn:
        assert await counter(conn, "runs_succeeded") - before == 1
        assert await refunds(conn, seeded.run_id) == 0, "a success keeps its credit"


@pytest.mark.parametrize("counter_name", ["pack_clicks", "pack_clickers"])
async def test_a_pack_counter_rolled_back_with_its_click_counts_nothing(
    counter_name: str, session: AsyncSession
) -> None:
    """AC-27: the click and its counts share the route's transaction; roll it back and neither the
    counts nor the account's first-click instant remain."""
    owner = await new_owner(session)
    await session.commit()
    click = RecordPackClick(
        SqlAlchemyCreditAccountRepository(session),
        SqlAlchemyDailyCounter(session),
        LoggingEventPublisher(),
    )
    before = await counter(session, counter_name)

    await click(RecordPackClickCommand(owner.user_id, Pack.PACK_3, _NOW().now()))
    assert await counter(session, counter_name) - before == 1, "the count exists inside the fact"
    await session.rollback()

    assert await counter(session, counter_name) == before
    assert (
        await scalar(
            session,
            "SELECT first_pack_click_at FROM billing_credit_account WHERE user_id = :u",
            u=owner.user_id.value,
        )
        is None
    )
