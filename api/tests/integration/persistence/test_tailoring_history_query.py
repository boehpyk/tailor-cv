"""Persistence tests for `SqlAlchemyTailoringHistoryQuery` (slice 2.3, T17, written after; AC-13's
SQL half, AC-55, AC-56's precondition, H-29, H-30).

T10's `tests/integration/tailoring/test_list_tailoring_history.py` stated the history contract
against an in-memory double; this file replays those scenarios against Postgres, where the order,
the keyset predicate and the two owner-conditioned `LEFT JOIN`s actually live:

- only the user's runs; newest first; a same-second tie ordered by id **descending in PostgreSQL's
  `uuid` order**; pages that neither skip nor repeat across such a tie; `next_cursor` exactly when a
  further entry exists;
- "CV deleted" derived at read time (`base_cv is None`, `base_cv_id` kept), and a dangling id that
  names **another user's** CV or posting never lends it to this history (the owner is in the join
  condition);
- the 140-character preview is computed in SQL, on code points, so multibyte text is cut where
  Python would cut it;
- a missing posting is logged as `tailoring.history_posting_missing` with ids only;
- the statement selects **no document column** (AC-55, by statement capture), and it can use
  `ix_tailoring_run_user_id_requested_at` (AC-56's precondition, by `EXPLAIN`).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.ownership import Owner, UserOwner
from tailorcraft.domain.intake.value_objects import BaseCvLabel
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingId, JobPostingText
from tailorcraft.domain.tailoring.history import HistoryCursor, HistoryPage, HistoryPageSize
from tailorcraft.domain.tailoring.value_objects import TailoredCv, TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.queries.tailoring_history import (
    SqlAlchemyTailoringHistoryQuery,
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
from tests.integration.owners import extracted_cv, pasted_posting, succeeded_run
from tests.integration.persistence.owner_rows import persist_guest, persist_user

_INDEX = "ix_tailoring_run_user_id_requested_at"


async def _persist_posting_for(
    session: AsyncSession, owner: Owner, job_posting_id: JobPostingId, at: datetime
) -> None:
    """A real posting at exactly `job_posting_id` — since 2.3 /verify (reviewer MINOR #1),
    `SqlAlchemyTailoringRunRepository.add` takes a run's posting `FOR KEY SHARE` and, through the
    request route's composition root, refuses a run whose posting does not exist."""
    await SqlAlchemyJobPostingRepository(session).add(
        JobPosting.from_pasted_text(
            id=job_posting_id, owner=owner, text=JobPostingText("posting " * 40), created_at=at
        )
    )


class _Rig:
    def __init__(self, session: AsyncSession, clock: FixedClock) -> None:
        self.session = session
        self.clock = clock
        self.runs = SqlAlchemyTailoringRunRepository(session)
        self.query = SqlAlchemyTailoringHistoryQuery(session)

    async def run_at(
        self, owner: UserOwner, at: datetime, run_id: UUID | None = None, **ids: object
    ) -> TailoringRunId:
        """When the caller does not name `job_posting_id` itself, a matching posting is created
        here so the repository's posting lock has a row to find — every caller that names one of
        its own (H-30's deliberately-missing posting via `run_at_with_orphan_posting`, a stranger's
        real posting, a posting built for the preview test) already handles it."""
        run = succeeded_run(
            owner, at, run_id=TailoringRunId(run_id) if run_id is not None else None, **ids
        )
        if "job_posting_id" not in ids:
            await _persist_posting_for(self.session, owner, run.job_posting_id, at)
        await self.runs.add(run)
        await self.session.flush()
        return run.id

    async def run_at_with_orphan_posting(
        self, owner: UserOwner, at: datetime, *, job_posting_id: JobPostingId
    ) -> TailoringRunId:
        """H-30: a run whose posting was never stored. `SqlAlchemyTailoringRunRepository.add`'s
        posting lock (2.3 /verify, reviewer MINOR #1) would refuse this, so it is inserted with
        Core SQL directly against the mapped table, bypassing the repository (and its lock)
        entirely — the only way left to build this state on purpose, the same technique
        `test_tailoring_run_repository.py`'s `_raw_insert` uses for the CHECK-constraint rows the
        aggregate itself cannot build."""
        run = succeeded_run(owner, at, job_posting_id=job_posting_id)
        documents = run.documents
        metrics = run.metrics
        await self.session.execute(
            tailoring_run_table.insert().values(
                id=run.id,
                guest_session_id=None,
                user_id=owner.user_id,
                base_cv_id=run.base_cv_id,
                job_posting_id=run.job_posting_id,
                status=run.status,
                failure_reason=run.failure_reason,
                tailored_cv=documents.cv if documents is not None else None,
                cover_letter=documents.cover_letter if documents is not None else None,
                model_name=metrics.model if metrics is not None else None,
                prompt_version=metrics.prompt_version if metrics is not None else None,
                prompt_tokens=metrics.prompt_tokens if metrics is not None else None,
                completion_tokens=metrics.completion_tokens if metrics is not None else None,
                llm_duration_ms=metrics.duration_ms if metrics is not None else None,
                requested_at=run.requested_at,
                started_at=run.started_at,
                completed_at=run.completed_at,
                version=run.version,
            )
        )
        await self.session.flush()
        return run.id

    async def page(
        self, owner: UserOwner, size: int, after: HistoryCursor | None = None
    ) -> HistoryPage:
        return await self.query.page_for_user(owner.user_id, after, HistoryPageSize(size))

    async def walk(self, owner: UserOwner, size: int) -> list[HistoryPage]:
        pages: list[HistoryPage] = []
        after: HistoryCursor | None = None
        while True:
            page = await self.page(owner, size, after)
            pages.append(page)
            if page.next_cursor is None:
                return pages
            after = page.next_cursor
            assert len(pages) < 50, "a cursor that does not advance"


def _ids(page: HistoryPage) -> list[TailoringRunId]:
    return [entry.tailoring_run_id for entry in page.entries]


# --- AC-13 against Postgres ------------------------------------------------------------------------


async def test_only_the_users_own_runs_newest_first(
    session: AsyncSession, clock: FixedClock
) -> None:
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    oldest = await rig.run_at(user, clock.now() - timedelta(hours=3))
    newest = await rig.run_at(user, clock.now() - timedelta(hours=1))
    middle = await rig.run_at(user, clock.now() - timedelta(hours=2))
    other_user = await persist_user(session, clock)
    other_user_run = succeeded_run(other_user, clock.now())
    await _persist_posting_for(session, other_user, other_user_run.job_posting_id, clock.now())
    await rig.runs.add(other_user_run)
    guest = await persist_guest(session, clock)
    guest_run = succeeded_run(guest, clock.now())
    await _persist_posting_for(session, guest, guest_run.job_posting_id, clock.now())
    await rig.runs.add(guest_run)
    await session.flush()

    page = await rig.page(user, 20)

    assert _ids(page) == [newest, middle, oldest]
    assert page.next_cursor is None


async def test_a_user_with_no_runs_gets_an_empty_page(
    session: AsyncSession, clock: FixedClock
) -> None:
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    await rig.run_at(await persist_user(session, clock), clock.now())

    assert await rig.page(user, 20) == HistoryPage(entries=(), next_cursor=None)


async def test_a_same_second_tie_is_ordered_by_uuid_descending(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The ids differ only in their first byte, so PostgreSQL's byte-wise `uuid` order and the
    domain's expectation must agree for this to pass."""
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    low = UUID("01000000-0000-7000-8000-000000000000")
    high = UUID("ff000000-0000-7000-8000-000000000000")
    await rig.run_at(user, clock.now(), low)
    await rig.run_at(user, clock.now(), high)

    page = await rig.page(user, 20)

    assert [run_id.value for run_id in _ids(page)] == [high, low]


async def test_a_full_page_carries_the_last_entrys_key_and_an_exhausting_one_does_not(
    session: AsyncSession, clock: FixedClock
) -> None:
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    for minutes in range(3):
        await rig.run_at(user, clock.now() - timedelta(minutes=minutes))

    two = await rig.page(user, 2)
    three = await rig.page(user, 3)

    last = two.entries[-1]
    assert two.next_cursor == HistoryCursor(
        requested_at=last.requested_at, tailoring_run_id=last.tailoring_run_id
    )
    assert len(three.entries) == 3
    assert three.next_cursor is None


async def test_paging_across_same_second_runs_returns_each_exactly_once(
    session: AsyncSession, clock: FixedClock
) -> None:
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    same_second = clock.now() - timedelta(minutes=5)
    created = [await rig.run_at(user, same_second) for _ in range(5)]
    later = await rig.run_at(user, clock.now())
    earlier = await rig.run_at(user, same_second - timedelta(seconds=1))

    pages = await rig.walk(user, 2)

    walked = [run_id for page in pages for run_id in _ids(page)]
    ties = sorted(created, key=lambda run_id: run_id.value, reverse=True)
    assert walked == [later, *ties, earlier]
    assert [len(page.entries) for page in pages] == [2, 2, 2, 1]


# --- The joins: "CV deleted", and no borrowing across owners ---------------------------------------


async def test_an_entry_names_its_cv_and_posting_while_they_exist(
    session: AsyncSession, clock: FixedClock
) -> None:
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    cv = extracted_cv(user, clock.now())
    cv.rename(BaseCvLabel("Backend roles"), clock.now())
    await SqlAlchemyBaseCvRepository(session).add(cv)
    posting = pasted_posting(user, clock.now())
    await SqlAlchemyJobPostingRepository(session).add(posting)
    await rig.run_at(user, clock.now(), base_cv_id=cv.id, job_posting_id=posting.id)

    (entry,) = (await rig.page(user, 20)).entries

    assert entry.base_cv is not None
    assert (entry.base_cv.base_cv_id, entry.base_cv.label, entry.base_cv.original_filename) == (
        cv.id,
        "Backend roles",
        "cv.pdf",
    )
    assert entry.posting is not None
    assert entry.posting.job_posting_id == posting.id
    assert entry.posting.preview == posting.text.value[:140]


async def test_a_deleted_cv_reads_as_none_with_its_id_kept(
    session: AsyncSession, clock: FixedClock
) -> None:
    """H-29 / plan §0.4(a): the reference dangles on purpose; "CV deleted" is derived here."""
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = extracted_cv(user, clock.now())
    await cvs.add(cv)
    await rig.run_at(user, clock.now(), base_cv_id=cv.id)
    await cvs.remove(cv.id, user)
    await session.flush()

    (entry,) = (await rig.page(user, 20)).entries

    assert entry.base_cv is None
    assert entry.base_cv_id == cv.id


async def test_another_users_cv_and_posting_at_a_dangling_id_are_not_lent(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The run names ids that belong to rows another user owns (impossible through the use cases,
    reachable through a dangling reference). The owner in each join condition keeps them out."""
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    stranger = await persist_user(session, clock)
    their_cv = extracted_cv(stranger, clock.now())
    their_cv.rename(BaseCvLabel("Not yours"), clock.now())
    await SqlAlchemyBaseCvRepository(session).add(their_cv)
    their_posting = pasted_posting(stranger, clock.now())
    await SqlAlchemyJobPostingRepository(session).add(their_posting)
    await rig.run_at(user, clock.now(), base_cv_id=their_cv.id, job_posting_id=their_posting.id)

    (entry,) = (await rig.page(user, 20)).entries

    assert entry.base_cv is None
    assert entry.posting is None


async def test_the_preview_is_140_code_points_of_multibyte_text(
    session: AsyncSession, clock: FixedClock
) -> None:
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    text = JobPostingText("Разработчик😀бэкенда " * 20)
    posting = JobPosting.from_pasted_text(
        id=JobPostingId(uuid7()), owner=user, text=text, created_at=clock.now()
    )
    await SqlAlchemyJobPostingRepository(session).add(posting)
    await rig.run_at(user, clock.now(), job_posting_id=posting.id)

    (entry,) = (await rig.page(user, 20)).entries

    assert entry.posting is not None
    assert entry.posting.preview == text.value[:140]
    assert len(entry.posting.preview) == 140
    assert len(entry.posting.preview.encode()) > 140


async def test_a_missing_posting_is_logged_with_ids_only(
    settings: Settings, session: AsyncSession, clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    """H-30: the entry is still listed (so its owner can delete it) and one warning names it —
    the run id and the posting id, nothing a row could leak."""
    configure_logging(settings)
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    missing = pasted_posting(user, clock.now())  # never stored
    run_id = await rig.run_at_with_orphan_posting(user, clock.now(), job_posting_id=missing.id)

    with caplog.at_level(logging.WARNING):
        (entry,) = (await rig.page(user, 20)).entries

    assert entry.tailoring_run_id == run_id
    assert entry.posting is None
    records = [r for r in caplog.records if "tailoring.history_posting_missing" in r.getMessage()]
    assert len(records) == 1
    payload = json.loads(records[0].getMessage())
    assert payload["tailoring_run_id"] == str(run_id.value)
    assert payload["job_posting_id"] == str(missing.id.value)
    carried = set(payload) - {"event", "level", "logger", "timestamp"}
    assert carried == {"tailoring_run_id", "job_posting_id"}


# --- AC-55: no document column is selected ----------------------------------------------------------

_DOCUMENT_COLUMNS = re.compile(
    r"\b(tailored_cv|cover_letter|edited_cv|edited_cover_letter|extracted_text)\b(?!_)"
)


@contextmanager
def _captured_statements(session: AsyncSession) -> Iterator[list[str]]:
    statements: list[str] = []
    engine = session.get_bind()

    def capture(conn: Connection, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


async def test_the_history_statement_selects_no_document_column(
    session: AsyncSession, clock: FixedClock
) -> None:
    """**Observed red, 2026-09-28**, with `_r.c.tailored_cv` added to the adapter's select list
    (restored byte-exact after): `AssertionError: assert ['tailored_cv'] == []`."""
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    posting = pasted_posting(user, clock.now())
    await SqlAlchemyJobPostingRepository(session).add(posting)
    run_id = await rig.run_at(user, clock.now(), job_posting_id=posting.id)
    stored = await rig.runs.get(run_id)
    stored.revise_cv(TailoredCv("e" * 500), expected_version=stored.version, at=clock.now())
    await session.flush()

    with _captured_statements(session) as statements:
        page = await rig.page(user, 20, HistoryCursor(clock.now() + timedelta(days=1), run_id))

    assert page.entries[0].edited is True  # the statement had an edited run to describe
    (statement,) = [s for s in statements if "tailoring_run" in s]
    select_list = statement.split(" FROM ", 1)[0]
    assert _DOCUMENT_COLUMNS.findall(select_list) == []
    assert select_list.count("p.text") == select_list.count("left(p.text")
    assert "left(p.text" in select_list


# --- AC-56's precondition: the index is usable -------------------------------------------------------


async def test_the_history_statement_can_use_the_user_index(
    session: AsyncSession, clock: FixedClock
) -> None:
    """With sequential scans disabled for this transaction only, the planner must still find a plan
    — through `ix_tailoring_run_user_id_requested_at`, both for the first page and past a cursor.
    (The measured budget on 500 runs is AC-56's, at T38.)

    **Observed red, 2026-09-28**, with `DROP INDEX ix_tailoring_run_user_id_requested_at` issued in
    this test's rolled-back transaction before the `EXPLAIN`: the plan fell back to a disabled-cost
    `Sort` over a scan (`cost=10000000027.98…`) and `'ix_tailoring_run_user_id_requested_at' in …`
    failed."""
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    run_id = await rig.run_at(user, clock.now())
    connection = await session.connection()
    captured: list[tuple[str, object]] = []

    def capture(
        conn: Connection, cursor: object, statement: str, parameters: object, *_: object
    ) -> None:
        if "tailoring_run" in statement and statement.lstrip().upper().startswith("SELECT"):
            captured.append((statement, parameters))

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        await rig.page(user, 20)
        await rig.page(user, 20, HistoryCursor(clock.now(), run_id))
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    await connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
    assert len(captured) == 2
    for statement, parameters in captured:
        plan = await connection.exec_driver_sql(f"EXPLAIN {statement}", parameters)  # type: ignore[arg-type]
        text_plan = "\n".join(row[0] for row in plan)
        assert _INDEX in text_plan, text_plan
        assert "Seq Scan on tailoring_run" not in text_plan
