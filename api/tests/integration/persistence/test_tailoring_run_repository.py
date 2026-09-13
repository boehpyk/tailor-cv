"""Persistence tests for `TailoringRun` — the imperative mapping, its `TypeDecorator`s and
`SqlAlchemyTailoringRunRepository` against real PostgreSQL (T33, written **after**).

Mirrors `test_job_posting_repository.py`'s and `test_base_cv_repository.py`'s structure: mapping
round-trip with `session.expunge_all()` to force a genuine reload, whole-second timestamp fidelity,
`NULL` round-trips, the cascade, the index, repository scoping. Three things are specific to this
aggregate and have no analogue in either sibling file:

- **Four statuses, not two or three** — `queued`, `running`, `succeeded` and `failed` each populate a
  different subset of the sixteen mapped columns, and TR-2/TR-5 (technical-plan.md) say precisely
  which subset. The round-trip test runs all four.
- **Three `CHECK` constraints, not one**, and the module docstring in
  `infrastructure/persistence/mapping/tailoring/tailoring_run.py` records that the first of the three
  was *strengthened* at T19 after a real Postgres accepted a non-succeeded row holding exactly one
  document under the original (weaker) form. Every pairing below is driven by a raw SQL `INSERT` or
  `UPDATE` — the aggregate itself makes every one of these states unconstructable, so raw SQL is the
  only way to reach what the constraints exist to refuse (the same reasoning
  `test_job_posting_repository.py`'s AC-21 tests give).
- **Two carried findings from T16**, named in the task list: `get`'s `from None` (a) and the
  `list_for_session` `id DESC` tiebreak on two runs sharing one whole second (b) — the in-memory fake
  has no tiebreak at all, and T15's own ordering test advances the clock deliberately, so nothing
  before this file exercises either.

**`list_stale_running` (V5f, /verify round 1, test-after)** — the stale-run sweep's query, added at
V5d-1 against `ix_tailoring_run_running_started_at`. The contract is `TailoringRunRepository.
list_stale_running`'s docstring; every test below states what that contract says should happen, not
what a first run of the query produced. Two things about it have no analogue anywhere else in this
file:

- **`started_at IS NULL` while `status = 'running'`** is a state the aggregate cannot build (only raw
  SQL, `_raw_insert`, reaches it — the same technique the CHECK-constraint tests above already use),
  and it must sort **first**, ahead of every run that has a timestamp — the query's `NULLS FIRST`
  fold, matching `TailoringRun.is_stale`'s.
- **The literal `'running'`**, not a bound parameter — measured at /verify round 1 with
  `EXPLAIN (GENERIC_PLAN)` and `enable_seqscan=off`: a bound `status = $1` proves nothing to the
  planner about `ix_tailoring_run_running_started_at`'s partial predicate and falls back to a
  sequential scan; `status = 'running'` lets it prove the `WHERE` implies the index and use an Index
  Scan. No row-level assertion can see this — the query returns the same rows either way — so the
  test intercepts the actual SQL text the adapter sends to Postgres for this call (the same
  `before_cursor_execute` hook SQLAlchemy itself uses) rather than reconstructing the statement by
  hand, per this agent's own instruction to prefer testing the real statement over a copy of it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.tailoring.errors import TailoringRunNotFound
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    LlmCallMetrics,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoredDocuments,
    TailoringFailureReason,
    TailoringRunId,
    TailoringRunStatus,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)

# `tailoring_run_table` is used below (the cascade test, matching `test_schema.py`'s and
# `test_job_posting_repository.py`'s pattern) — and importing this module is also what runs
# `mapper_registry.map_imperatively(TailoringRun, ...)` as an import side effect. Without it
# `TailoringRun._id` etc. do not exist yet and every repository import below fails at collection
# time with `AttributeError: type object 'TailoringRun' has no attribute '_id'`.
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.settings import Settings

# --- Test helpers --------------------------------------------------------------------------------


async def _persist_owner(
    session: AsyncSession, clock: FixedClock, *, token_hash: str
) -> GuestSession:
    """A `TailoringRun` needs a real, persisted `GuestSession` to satisfy the `NOT NULL` FK
    (`tailoring_run.guest_session_id`) — every test below builds one first."""
    repo = SqlAlchemyGuestSessionRepository(session)
    owner = GuestSession.start(
        id=repo.next_identity(), token_hash=token_hash, at=clock.now(), ttl_hours=24
    )
    await repo.add(owner)
    return owner


def _documents() -> TailoredDocuments:
    """Comfortably past both floors (400 / 200 non-whitespace characters) — the exact values do not
    matter to these tests, only that both round-trip intact."""
    return TailoredDocuments(cv=TailoredCv("a" * 450), cover_letter=CoverLetter("b" * 250))


def _metrics() -> LlmCallMetrics:
    return LlmCallMetrics(
        model=ModelName("gemini-test"),
        prompt_version=PromptVersion("1"),
        prompt_tokens=111,
        completion_tokens=222,
        duration_ms=1234,
    )


def _queued(
    runs: SqlAlchemyTailoringRunRepository, owner_id: GuestSessionId, clock: FixedClock
) -> TailoringRun:
    return TailoringRun.request(
        id=runs.next_identity(),
        guest_session_id=owner_id,
        base_cv_id=BaseCvId(value=uuid4()),
        job_posting_id=JobPostingId(value=uuid4()),
        requested_at=clock.now(),
    )


def _running(
    runs: SqlAlchemyTailoringRunRepository, owner_id: GuestSessionId, clock: FixedClock
) -> TailoringRun:
    run = _queued(runs, owner_id, clock)
    run.mark_started(clock.now())
    return run


def _succeeded(
    runs: SqlAlchemyTailoringRunRepository, owner_id: GuestSessionId, clock: FixedClock
) -> TailoringRun:
    run = _running(runs, owner_id, clock)
    run.mark_succeeded(_documents(), _metrics(), clock.now())
    return run


def _failed(
    runs: SqlAlchemyTailoringRunRepository,
    owner_id: GuestSessionId,
    clock: FixedClock,
    *,
    reason: TailoringFailureReason = TailoringFailureReason.LLM_UNAVAILABLE,
) -> TailoringRun:
    run = _queued(runs, owner_id, clock)
    run.mark_failed(reason, clock.now())
    return run


async def _raw_insert(
    session: AsyncSession, owner_id: GuestSessionId, clock: FixedClock, **overrides: object
) -> None:
    """Insert one `tailoring_run` row by hand, bypassing the aggregate entirely — the only way to
    reach a state the three `CHECK` constraints exist to refuse (the mapping module's own docstring:
    "a bad backfill is not bound by Python"). `defaults` is an otherwise-legal `queued` row; each
    test overrides only the column(s) that pairing is about, so every OTHER constraint stays
    satisfied and the `IntegrityError` raised is unambiguously the one under test.
    """
    defaults: dict[str, object] = {
        "id": uuid4(),
        "guest_session_id": owner_id.value,
        "base_cv_id": uuid4(),
        "job_posting_id": uuid4(),
        "status": "queued",
        "failure_reason": None,
        "tailored_cv": None,
        "cover_letter": None,
        "model_name": None,
        "prompt_version": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "llm_duration_ms": None,
        "requested_at": clock.now(),
        "started_at": None,
        "completed_at": None,
    }
    defaults.update(overrides)
    await session.execute(
        text(
            "INSERT INTO tailoring_run "
            "(id, guest_session_id, base_cv_id, job_posting_id, status, failure_reason, "
            "tailored_cv, cover_letter, model_name, prompt_version, prompt_tokens, "
            "completion_tokens, llm_duration_ms, requested_at, started_at, completed_at) "
            "VALUES (:id, :guest_session_id, :base_cv_id, :job_posting_id, :status, "
            ":failure_reason, :tailored_cv, :cover_letter, :model_name, :prompt_version, "
            ":prompt_tokens, :completion_tokens, :llm_duration_ms, :requested_at, "
            ":started_at, :completed_at)"
        ),
        defaults,
    )


# --- Mapping round-trip: every value object comes back as its own type, for ALL FOUR statuses -----


async def test_round_trip_of_a_queued_run_preserves_value_object_types_and_nulls(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="1" * 64)
    runs = SqlAlchemyTailoringRunRepository(session)
    run = _queued(runs, owner.id, clock)
    await runs.add(run)

    session.expunge_all()
    reloaded = await runs.get(run.id)

    assert isinstance(reloaded.id, TailoringRunId)
    assert reloaded.id == run.id
    assert isinstance(reloaded.guest_session_id, GuestSessionId)
    assert reloaded.guest_session_id == owner.id
    assert isinstance(reloaded.base_cv_id, BaseCvId)
    assert reloaded.base_cv_id == run.base_cv_id
    assert isinstance(reloaded.job_posting_id, JobPostingId)
    assert reloaded.job_posting_id == run.job_posting_id
    # An enum MEMBER, not a str that merely compares equal — `TailoringRun`'s own transition guards
    # are written with `is`/`in` against enum members, so a loaded row that came back as a bare `str`
    # would refuse every legal transition while `==` elsewhere kept insisting it was fine
    # (`TailoringRunStatusType`'s docstring).
    assert reloaded.status is TailoringRunStatus.QUEUED
    assert reloaded.failure_reason is None
    assert reloaded.documents is None
    assert reloaded.metrics is None
    assert reloaded.started_at is None
    assert reloaded.completed_at is None


async def test_round_trip_of_a_running_run_preserves_value_object_types(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="2" * 64)
    runs = SqlAlchemyTailoringRunRepository(session)
    run = _running(runs, owner.id, clock)
    await runs.add(run)

    session.expunge_all()
    reloaded = await runs.get(run.id)

    assert reloaded.status is TailoringRunStatus.RUNNING
    assert reloaded.started_at is not None
    assert reloaded.started_at == run.started_at
    assert reloaded.failure_reason is None
    assert reloaded.documents is None
    assert reloaded.metrics is None
    assert reloaded.completed_at is None


async def test_round_trip_of_a_succeeded_run_preserves_value_object_types_and_both_documents(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="3" * 64)
    runs = SqlAlchemyTailoringRunRepository(session)
    run = _succeeded(runs, owner.id, clock)
    await runs.add(run)

    session.expunge_all()
    reloaded = await runs.get(run.id)

    assert reloaded.status is TailoringRunStatus.SUCCEEDED
    assert reloaded.failure_reason is None
    # Both assembling properties come back equal — `TailoredDocuments` and `LlmCallMetrics` are
    # frozen dataclasses compared by value, so this is a genuine round-trip proof, not merely
    # "something non-None came back" (OQ-5, the mapping module's own docstring).
    assert isinstance(reloaded.documents, TailoredDocuments)
    assert reloaded.documents == run.documents
    assert isinstance(reloaded.metrics, LlmCallMetrics)
    assert reloaded.metrics == run.metrics
    assert reloaded.completed_at is not None
    assert reloaded.completed_at == run.completed_at


async def test_round_trip_of_a_failed_run_preserves_the_failure_reason_and_nulls_the_documents(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="4" * 64)
    runs = SqlAlchemyTailoringRunRepository(session)
    run = _failed(runs, owner.id, clock, reason=TailoringFailureReason.LLM_OUTPUT_INVALID)
    await runs.add(run)

    session.expunge_all()
    reloaded = await runs.get(run.id)

    assert reloaded.status is TailoringRunStatus.FAILED
    assert reloaded.failure_reason is TailoringFailureReason.LLM_OUTPUT_INVALID
    assert reloaded.documents is None
    assert reloaded.metrics is None
    assert reloaded.completed_at is not None
    assert reloaded.completed_at == run.completed_at
    # A failed run legitimately never started (G-14/G-25 both fail from `queued`) — the round trip
    # must preserve that `None` rather than inventing a `started_at` to satisfy some other reader.
    assert reloaded.started_at is None


# --- Whole-second timestamp fidelity (ADR-0007), across all three timestamp columns ----------------


async def test_round_trip_preserves_whole_second_requested_started_and_completed_at(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="5" * 64)
    runs = SqlAlchemyTailoringRunRepository(session)
    run = _succeeded(runs, owner.id, clock)
    await runs.add(run)

    session.expunge_all()
    reloaded = await runs.get(run.id)

    assert reloaded.requested_at.microsecond == 0
    assert reloaded.requested_at == run.requested_at
    assert reloaded.started_at is not None
    assert reloaded.started_at.microsecond == 0
    assert reloaded.started_at == run.started_at
    assert reloaded.completed_at is not None
    assert reloaded.completed_at.microsecond == 0
    assert reloaded.completed_at == run.completed_at


# --- NULL round-trips for every optional column (a `queued` row is the ordinary case, not the -----
# --- edge case: `TailoredCvType`'s own docstring is explicit that this must not raise) -------------


async def test_a_queued_run_round_trips_null_for_every_optional_column(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="6" * 64)
    runs = SqlAlchemyTailoringRunRepository(session)
    run = _queued(runs, owner.id, clock)
    await runs.add(run)

    session.expunge_all()
    reloaded = await runs.get(run.id)

    assert reloaded.failure_reason is None
    assert reloaded.documents is None  # tailored_cv AND cover_letter
    assert reloaded.metrics is None  # model_name, prompt_version, both token counts, duration_ms
    assert reloaded.started_at is None
    assert reloaded.completed_at is None


# --- The three CHECK constraints reject all four AC-4 bad pairings, plus the fifth T19 added -------


async def test_check_constraint_rejects_a_succeeded_row_missing_the_tailored_cv(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="7" * 64)

    with pytest.raises(IntegrityError, match="ck_tailoring_run_documents_match_status"):
        await _raw_insert(
            session,
            owner.id,
            clock,
            status="succeeded",
            tailored_cv=None,
            cover_letter="x" * 250,
            completed_at=clock.now(),
        )


async def test_check_constraint_rejects_a_succeeded_row_missing_the_cover_letter(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="8" * 64)

    with pytest.raises(IntegrityError, match="ck_tailoring_run_documents_match_status"):
        await _raw_insert(
            session,
            owner.id,
            clock,
            status="succeeded",
            tailored_cv="x" * 450,
            cover_letter=None,
            completed_at=clock.now(),
        )


async def test_check_constraint_rejects_a_non_succeeded_row_holding_both_documents(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The mirror pairing: a `queued` row that somehow holds two documents already."""
    owner = await _persist_owner(session, clock, token_hash="9" * 64)

    with pytest.raises(IntegrityError, match="ck_tailoring_run_documents_match_status"):
        await _raw_insert(
            session,
            owner.id,
            clock,
            status="queued",
            tailored_cv="x" * 450,
            cover_letter="y" * 250,
        )


async def test_check_constraint_rejects_a_non_succeeded_row_holding_exactly_one_document(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The FIFTH pairing, added at T19 — and the one the original (weaker) constraint form let
    through. Measured against a real Postgres during T19: `UPDATE tailoring_run SET cover_letter =
    'x'` succeeded on a `failed` row under the old `(status='succeeded') = (tailored_cv IS NOT NULL
    AND cover_letter IS NOT NULL)` form, because a row holding exactly one of the two documents makes
    that conjunction `false`, and `false = false` (non-succeeded) is satisfied.

    Reproduced here exactly as the mapping module's own docstring describes it: a legal `failed` row
    inserted first (both documents `NULL`, as `mark_failed` always leaves them), then a raw `UPDATE`
    that gives it exactly one. TR-5 — "a succeeded run has both documents; half a result is a
    failure" — must hold of the row as well as of the object, and the amended, written-once-per-
    column constraint is what makes that true where the aggregate cannot reach.
    """
    owner = await _persist_owner(session, clock, token_hash="a" * 64)
    run_id = uuid4()
    await _raw_insert(
        session,
        owner.id,
        clock,
        id=run_id,
        status="failed",
        failure_reason="llm_unavailable",
        completed_at=clock.now(),
    )

    with pytest.raises(IntegrityError, match="ck_tailoring_run_documents_match_status"):
        await session.execute(
            text("UPDATE tailoring_run SET cover_letter = :cover_letter WHERE id = :id"),
            {"cover_letter": "x" * 250, "id": run_id},
        )


async def test_check_constraint_rejects_a_failed_row_missing_the_failure_reason(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="b" * 64)

    with pytest.raises(IntegrityError, match="ck_tailoring_run_failure_reason_matches_status"):
        await _raw_insert(
            session, owner.id, clock, status="failed", failure_reason=None, completed_at=clock.now()
        )


async def test_check_constraint_rejects_a_non_failed_row_carrying_a_failure_reason(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="c" * 64)

    with pytest.raises(IntegrityError, match="ck_tailoring_run_failure_reason_matches_status"):
        await _raw_insert(
            session, owner.id, clock, status="queued", failure_reason="llm_unavailable"
        )


async def test_check_constraint_rejects_a_terminal_row_missing_completed_at(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="d" * 64)

    with pytest.raises(
        IntegrityError, match="ck_tailoring_run_completed_at_matches_terminal_status"
    ):
        await _raw_insert(
            session,
            owner.id,
            clock,
            status="succeeded",
            tailored_cv="x" * 450,
            cover_letter="y" * 250,
            completed_at=None,
        )


async def test_check_constraint_rejects_a_non_terminal_row_carrying_completed_at(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="e" * 64)

    with pytest.raises(
        IntegrityError, match="ck_tailoring_run_completed_at_matches_terminal_status"
    ):
        await _raw_insert(session, owner.id, clock, status="queued", completed_at=clock.now())


# --- AC-27: deleting a guest session cascades to its tailoring runs --------------------------------


async def test_deleting_a_guest_session_cascades_to_its_tailoring_runs(
    session: AsyncSession, clock: FixedClock
) -> None:
    """This is what lets the 1.6 purge reach `tailoring_run` with **no new predicate**: it stays
    `identity_guest_session.expires_at < now()` and nothing else. Two runs, one session, one delete —
    both rows (and their documents) must be gone in the same statement."""
    owner = await _persist_owner(session, clock, token_hash="f" * 64)
    runs = SqlAlchemyTailoringRunRepository(session)
    first = _queued(runs, owner.id, clock)
    await runs.add(first)
    second = _succeeded(runs, owner.id, clock)
    await runs.add(second)

    await session.execute(guest_session_table.delete().where(guest_session_table.c.id == owner.id))
    await session.flush()

    # `tailoring_run.id` is `TypeDecorator`-backed (`TailoringRunIdType`), so the bound parameter is
    # the domain value object the decorator's `process_bind_param` expects — not the bare `UUID`
    # underneath it, the same lesson `test_job_posting_repository.py`'s cascade test needed.
    remaining = await session.execute(
        select(tailoring_run_table.c.id).where(tailoring_run_table.c.id.in_([first.id, second.id]))
    )
    assert remaining.scalars().all() == []


# --- The index exists -------------------------------------------------------------------------------


async def test_guest_session_id_index_exists_on_tailoring_run(session: AsyncSession) -> None:
    """Serves `GET /api/tailoring-runs`, `count_for_session`, `find_active_for_session` and the
    cascade above."""
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'tailoring_run' "
            "AND indexname = 'ix_tailoring_run_guest_session_id'"
        )
    )
    assert result.scalar_one_or_none() == "ix_tailoring_run_guest_session_id"


# --- Carried from T16 (a): `get` raises with NO chained cause -------------------------------------


async def test_get_for_an_unknown_id_raises_not_found_with_no_chained_cause(
    session: AsyncSession,
) -> None:
    """T15's negative `__cause__` assertion (`test_read_tailoring_runs.py`'s
    `test_get_for_a_nonexistent_id_raises_tailoring_run_not_found_without_ownership_cause`) only
    discriminates "absent" from "not mine" if the REAL repository's `get` keeps `from None` — that
    test drives `GetTailoringRunForSession` against `FakeTailoringRunRepository`, which does this by
    construction. This is the sibling proof, against the real adapter: if `SqlAlchemyTailoringRunRepository
    .get` ever let a `NoResultFound` (or anything else) chain through, the pairing above would
    silently stop proving anything and nothing would fail — see the repository's own comment on this
    exact `raise ... from None`.
    """
    runs = SqlAlchemyTailoringRunRepository(session)

    with pytest.raises(TailoringRunNotFound) as exc_info:
        await runs.get(runs.next_identity())

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


# --- Carried from T16 (b): the `id DESC` tiebreak in `list_for_session` ----------------------------


async def test_list_for_session_breaks_a_tie_on_the_same_second_by_id_descending(
    session: AsyncSession, clock: FixedClock
) -> None:
    """Two runs requested inside the SAME whole second — ordinary under the `Clock`'s whole-second
    contract (ADR-0007), not exotic. T15's `test_list_returns_runs_newest_first` advances the clock
    between every run it creates and so never exercises this branch, and the in-memory fake has no
    tiebreak logic at all (`FakeTailoringRunRepository.list_for_session` sorts on `requested_at`
    alone). `TailoringRunId` is a UUIDv7, so a real millisecond gap between the two inserts — well
    under one whole second — is enough to make the second id unambiguously greater while
    `requested_at` (whole-second) stays identical, exactly the case the repository's own docstring
    names.
    """
    owner = await _persist_owner(session, clock, token_hash="1a" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)

    older = _queued(runs, owner.id, clock)
    await runs.add(older)
    await asyncio.sleep(
        0.002
    )  # cross a millisecond boundary so the newer id is unambiguously greater
    newer = _queued(runs, owner.id, clock)
    await runs.add(newer)

    assert older.requested_at == newer.requested_at, (
        "the tiebreak only means something if the two runs share the same whole second"
    )
    assert newer.id.value > older.id.value, "sanity: uuid7 time-ordering held for this pair"

    result = await runs.list_for_session(owner.id)

    assert [run.id for run in result] == [newer.id, older.id]


# --- count_for_session and find_active_for_session against the real database -----------------------


async def test_count_for_session_counts_only_that_sessions_rows(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner_a = await _persist_owner(session, clock, token_hash="1b" * 32)
    owner_b = await _persist_owner(session, clock, token_hash="1c" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)

    await runs.add(_queued(runs, owner_a.id, clock))
    await runs.add(_succeeded(runs, owner_a.id, clock))
    await runs.add(_queued(runs, owner_b.id, clock))

    assert await runs.count_for_session(owner_a.id) == 2
    assert await runs.count_for_session(owner_b.id) == 1


async def test_find_active_for_session_returns_none_when_every_run_is_terminal(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="1d" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    await runs.add(_succeeded(runs, owner.id, clock))
    await runs.add(_failed(runs, owner.id, clock))

    assert await runs.find_active_for_session(owner.id) is None


async def test_find_active_for_session_finds_a_queued_run(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="1e" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    active = _queued(runs, owner.id, clock)
    await runs.add(active)
    await runs.add(_succeeded(runs, owner.id, clock))  # a decoy, must not be returned

    found = await runs.find_active_for_session(owner.id)

    assert found is not None
    assert found.id == active.id


async def test_find_active_for_session_finds_a_running_run(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="1f" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    active = _running(runs, owner.id, clock)
    await runs.add(active)
    await runs.add(_failed(runs, owner.id, clock))  # a decoy, must not be returned

    found = await runs.find_active_for_session(owner.id)

    assert found is not None
    assert found.id == active.id


async def test_find_active_for_session_with_two_active_runs_returns_the_newest_without_raising(
    session: AsyncSession, clock: FixedClock
) -> None:
    """ADR-0014 §4: the one-active-run rule is a SOFT use-case check (`RequestTailoringRun`'s own
    guard, G-9), not a database constraint — two genuinely concurrent requests may both pass it, so
    the repository itself must tolerate two active rows for one session rather than treating that as
    a data-integrity impossibility. `find_active_for_session` uses `ORDER BY ... LIMIT 1`, not
    `scalar_one_or_none()`, for exactly this reason (the repository's own docstring); this is the
    proof that the accepted race does not turn into a `MultipleResultsFound` at the one moment the
    user is already confused by having two runs in flight.
    """
    owner = await _persist_owner(session, clock, token_hash="20" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    older_active = _queued(runs, owner.id, clock)
    await runs.add(older_active)
    await asyncio.sleep(0.002)
    newer_active = _running(runs, owner.id, clock)
    await runs.add(newer_active)

    found = await runs.find_active_for_session(owner.id)

    assert found is not None
    assert found.id == newer_active.id


# --- list_stale_running (V5f, /verify round 1): the stale-run sweep's query --------------------------


def _running_at(
    runs: SqlAlchemyTailoringRunRepository,
    owner_id: GuestSessionId,
    *,
    requested_at: datetime,
    started_at: datetime,
) -> TailoringRun:
    """A `RUNNING` run with explicit, independent `requested_at`/`started_at` instants.

    Unlike `_running` above (which always starts a run at `clock.now()`), `list_stale_running`'s own
    tests need `started_at` placed at an arbitrary point in the past relative to a chosen cutoff, not
    "now" — the sweep's whole premise is a run whose worker vanished a while ago.
    """
    run = TailoringRun.request(
        id=runs.next_identity(),
        guest_session_id=owner_id,
        base_cv_id=BaseCvId(value=uuid4()),
        job_posting_id=JobPostingId(value=uuid4()),
        requested_at=requested_at,
    )
    run.mark_started(started_at)
    return run


async def test_list_stale_running_selects_a_running_run_started_before_the_cutoff(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="21" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    stale = _running_at(
        runs,
        owner.id,
        requested_at=clock.now() - timedelta(seconds=1_000),
        started_at=clock.now() - timedelta(seconds=400),
    )
    await runs.add(stale)

    result = await runs.list_stale_running(
        started_before=clock.now() - timedelta(seconds=300), limit=10
    )

    assert [run.id for run in result] == [stale.id]


async def test_list_stale_running_excludes_a_running_run_started_after_the_cutoff(
    session: AsyncSession, clock: FixedClock
) -> None:
    """A decoy fresh `RUNNING` run must not be selected — and a genuinely stale run in the same call
    proves `list_stale_running` actually ran rather than merely returning an empty list by accident."""
    owner = await _persist_owner(session, clock, token_hash="22" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    fresh = _running_at(
        runs,
        owner.id,
        requested_at=clock.now() - timedelta(seconds=10),
        started_at=clock.now() - timedelta(seconds=5),
    )
    stale = _running_at(
        runs,
        owner.id,
        requested_at=clock.now() - timedelta(seconds=1_000),
        started_at=clock.now() - timedelta(seconds=400),
    )
    await runs.add(fresh)
    await runs.add(stale)

    result = await runs.list_stale_running(
        started_before=clock.now() - timedelta(seconds=300), limit=10
    )

    assert [run.id for run in result] == [stale.id]


async def test_list_stale_running_excludes_a_run_started_exactly_at_the_cutoff(
    session: AsyncSession, clock: FixedClock
) -> None:
    """Strict `<`, matching `TailoringRun.is_stale`'s strict `>`: a run started exactly at the
    boundary the caller passes is still fresh and must not be listed at all."""
    owner = await _persist_owner(session, clock, token_hash="23" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    cutoff = clock.now() - timedelta(seconds=300)
    boundary = _running_at(
        runs, owner.id, requested_at=clock.now() - timedelta(seconds=1_000), started_at=cutoff
    )
    stale = _running_at(
        runs,
        owner.id,
        requested_at=clock.now() - timedelta(seconds=1_000),
        started_at=clock.now() - timedelta(seconds=400),
    )
    await runs.add(boundary)
    await runs.add(stale)

    result = await runs.list_stale_running(started_before=cutoff, limit=10)

    assert [run.id for run in result] == [stale.id]


async def test_list_stale_running_excludes_queued_and_terminal_runs_however_old(
    session: AsyncSession, clock: FixedClock
) -> None:
    """A `queued` run, a `succeeded` run and a `failed` run — every one of them old enough to be
    stale if status were ignored — must all be excluded. A genuinely stale `running` run in the same
    call proves the query ran."""
    owner = await _persist_owner(session, clock, token_hash="24" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    very_old = clock.now() - timedelta(days=1)
    queued = TailoringRun.request(
        id=runs.next_identity(),
        guest_session_id=owner.id,
        base_cv_id=BaseCvId(value=uuid4()),
        job_posting_id=JobPostingId(value=uuid4()),
        requested_at=very_old,
    )
    succeeded = _running_at(runs, owner.id, requested_at=very_old, started_at=very_old)
    succeeded.mark_succeeded(_documents(), _metrics(), very_old + timedelta(seconds=1))
    failed = _running_at(runs, owner.id, requested_at=very_old, started_at=very_old)
    failed.mark_failed(TailoringFailureReason.LLM_ERROR, very_old + timedelta(seconds=1))
    stale = _running_at(
        runs,
        owner.id,
        requested_at=clock.now() - timedelta(seconds=1_000),
        started_at=clock.now() - timedelta(seconds=400),
    )
    for run in (queued, succeeded, failed, stale):
        await runs.add(run)

    result = await runs.list_stale_running(
        started_before=clock.now() - timedelta(seconds=300), limit=10
    )

    assert [run.id for run in result] == [stale.id]


async def test_list_stale_running_orders_oldest_started_at_first_with_an_id_tiebreak(
    session: AsyncSession, clock: FixedClock
) -> None:
    """Oldest `started_at` first; two runs sharing the same whole second break the tie on `id` —
    ordinary under the `Clock`'s whole-second contract, matching `list_for_session`'s own tiebreak
    test above."""
    owner = await _persist_owner(session, clock, token_hash="25" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    same_second = clock.now() - timedelta(seconds=400)
    older = _running_at(
        runs, owner.id, requested_at=clock.now() - timedelta(seconds=1_000), started_at=same_second
    )
    await runs.add(older)
    await asyncio.sleep(
        0.002
    )  # cross a millisecond boundary so the newer id is unambiguously greater
    newer = _running_at(
        runs, owner.id, requested_at=clock.now() - timedelta(seconds=1_000), started_at=same_second
    )
    await runs.add(newer)
    oldest = _running_at(
        runs,
        owner.id,
        requested_at=clock.now() - timedelta(seconds=2_000),
        started_at=clock.now() - timedelta(seconds=900),
    )
    await runs.add(oldest)

    assert older.started_at == newer.started_at, (
        "the tiebreak only means something on a shared second"
    )
    assert newer.id.value > older.id.value, "sanity: uuid7 time-ordering held for this pair"

    result = await runs.list_stale_running(
        started_before=clock.now() - timedelta(seconds=300), limit=10
    )

    assert [run.id for run in result] == [oldest.id, older.id, newer.id]


async def test_list_stale_running_respects_the_limit(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="26" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    oldest = _running_at(
        runs,
        owner.id,
        requested_at=clock.now() - timedelta(seconds=2_000),
        started_at=clock.now() - timedelta(seconds=900),
    )
    middle = _running_at(
        runs,
        owner.id,
        requested_at=clock.now() - timedelta(seconds=1_500),
        started_at=clock.now() - timedelta(seconds=700),
    )
    newest = _running_at(
        runs,
        owner.id,
        requested_at=clock.now() - timedelta(seconds=1_000),
        started_at=clock.now() - timedelta(seconds=400),
    )
    for run in (newest, oldest, middle):  # added out of order on purpose
        await runs.add(run)

    result = await runs.list_stale_running(
        started_before=clock.now() - timedelta(seconds=300), limit=2
    )

    assert [run.id for run in result] == [oldest.id, middle.id]


async def test_list_stale_running_selects_a_null_started_at_running_row_first(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`started_at IS NULL` while `status = 'running'` is a state the aggregate cannot build — the
    public API always sets both together (`mark_started`) — so the only way to reach it is a raw
    `INSERT`, exactly as the CHECK-constraint tests above reach their own unconstructable states. The
    repository's own docstring folds this to "stale, and the most suspect row of all": a run that
    cannot say when it started sorts **first**, ahead of a run that merely started a while ago.
    """
    owner = await _persist_owner(session, clock, token_hash="27" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    with_timestamp = _running_at(
        runs,
        owner.id,
        requested_at=clock.now() - timedelta(seconds=1_000),
        started_at=clock.now() - timedelta(seconds=400),
    )
    await runs.add(with_timestamp)
    null_started_at_id = uuid4()
    await _raw_insert(
        session,
        owner.id,
        clock,
        id=null_started_at_id,
        status="running",
        started_at=None,
    )

    result = await runs.list_stale_running(
        started_before=clock.now() - timedelta(seconds=300), limit=10
    )

    assert [run.id.value for run in result] == [null_started_at_id, with_timestamp.id.value]
    assert result[0].started_at is None


async def test_list_stale_running_sends_status_to_postgres_as_a_literal_not_a_bound_parameter(
    engine: AsyncEngine, session: AsyncSession, clock: FixedClock
) -> None:
    """Pins the adapter's own comment: `status` must reach Postgres as the literal `'running'`
    (`literal_execute=True`), never as a bound parameter, or the planner cannot prove the query's
    `WHERE` implies `ix_tailoring_run_running_started_at`'s partial predicate and silently falls back
    to a sequential scan on a table that keeps every run ever made (measured with
    `EXPLAIN (GENERIC_PLAN)` / `enable_seqscan=off`, per the mapping module's own docstring). No
    returned row can tell the two apart — this intercepts the actual SQL text the adapter sends to
    Postgres for this call, the real statement rather than a hand-rebuilt copy of it.

    **Proven to discriminate**: removing `literal_execute=True` from `list_stale_running` locally (so
    the comparison reads `_TAILORING_RUN_STATUS == running.value` or similar) replaces the literal
    with a bound placeholder and turns this assertion red — reported alongside this file's other
    guards.
    """
    owner = await _persist_owner(session, clock, token_hash="28" * 32)
    runs = SqlAlchemyTailoringRunRepository(session)
    await runs.add(
        _running_at(
            runs,
            owner.id,
            requested_at=clock.now() - timedelta(seconds=1_000),
            started_at=clock.now() - timedelta(seconds=400),
        )
    )

    captured_statements: list[str] = []

    def _capture(conn: object, cursor: object, statement: str, *_args: object) -> None:
        captured_statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        await runs.list_stale_running(started_before=clock.now() - timedelta(seconds=300), limit=10)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    matching = [s for s in captured_statements if "tailoring_run" in s and "started_at" in s]
    assert matching, "list_stale_running's SELECT never reached the database"
    assert "'running'" in matching[-1], (
        "the status must be rendered as the literal 'running', not a bound parameter — see this "
        "test's own docstring"
    )


async def test_ix_tailoring_run_running_started_at_exists_with_its_partial_predicate(
    session: AsyncSession,
) -> None:
    """Mirrors `test_guest_session_id_index_exists_on_tailoring_run` above, for the partial index
    the sweep depends on (migration `3d0b70b7837f`)."""
    result = await session.execute(
        text(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'tailoring_run' "
            "AND indexname = 'ix_tailoring_run_running_started_at'"
        )
    )
    indexdef = result.scalar_one_or_none()
    assert indexdef is not None
    assert "started_at" in indexdef
    assert "WHERE" in indexdef
    assert "running" in indexdef


def test_migration_3d0b70b7837f_downgrade_removes_the_partial_index(settings: Settings) -> None:
    """Run as a plain `def test_...`, not `async def` — exactly the reason
    `test_tailoring_task.py`'s two literal-bridge tests are plain `def`s: `alembic/env.py`'s
    `run_migrations_online` wraps every online migration in its own `asyncio.run(
    run_async_migrations())`, and `asyncio.run` refuses to start a second loop on top of one already
    running. pytest-asyncio's session-scoped loop is only ever *running* while an `async def` test's
    own body executes — idle here, which is where Alembic's own fresh `asyncio.run()` may open one.

    Downgrades then re-upgrades within this one test, in a `finally`: this suite has ONE test
    database, migrated to head ONCE per session (`conftest.py`'s `_migrated`), so leaving the schema
    at `-1` would silently break every test that runs after this one — in this file and beyond, since
    `_migrated` never runs a second time. Requests no `session`/`connection` fixture: those bind to a
    SAVEPOINT held open on the same shared connection for the rest of the test, and Alembic's own DDL
    needs to run outside of any such transaction rather than risk lock contention with it.
    """
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.test_database_url)

    async def _index_exists() -> bool:
        verify_engine = create_async_engine(settings.test_database_url, poolclass=None)
        try:
            async with verify_engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE tablename = 'tailoring_run' "
                        "AND indexname = 'ix_tailoring_run_running_started_at'"
                    )
                )
                return result.scalar_one_or_none() is not None
        finally:
            await verify_engine.dispose()

    assert asyncio.run(_index_exists()) is True, (
        "the index must exist at head before this test runs"
    )

    command.downgrade(config, "-1")
    try:
        assert asyncio.run(_index_exists()) is False, (
            "downgrade() must drop ix_tailoring_run_running_started_at — proven to discriminate by "
            "commenting out the op.drop_index() call in the migration's downgrade() locally"
        )
    finally:
        command.upgrade(config, "head")
        assert asyncio.run(_index_exists()) is True, (
            "the schema must be back at head before the next test in the session runs"
        )
