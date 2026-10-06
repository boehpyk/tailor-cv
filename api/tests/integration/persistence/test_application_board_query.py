"""Persistence tests for `SqlAlchemyApplicationBoardQuery` (slice 3.1, T19, test-after; AC-30's
persistence half, AC-31).

`tests/integration/tracking/test_show_application_board.py` states the use case against an in-memory
double; this file replays the **contract** against PostgreSQL, where the order and the three
owner-conditioned `LEFT JOIN`s actually live (the shape of 2.3's `test_tailoring_history_query.py`):

- a user's cards only; ordered `stage_changed_at DESC, id DESC` — a same-second tie by id descending
  in PostgreSQL's `uuid` order; an empty board is an empty tuple;
- every field of a card, and its run (`requested_at`, `edited`), posting and CV, while they exist;
- "CV deleted" derived at read time (`base_cv is None`, the card still listed), and an id that names
  **another user's** CV or posting is never lent to this board (the owner is in the join condition);
- a card whose run row is missing — only raw SQL builds one — is **still listed**, with `run`,
  `posting` and `base_cv` all `None`, and one `tracking.board_run_missing` warning carrying the card's
  id and nothing a row could leak;
- the 140-character preview is computed in SQL on code points;
- **AC-31**: the statement selects no document column and no posting text beyond `left(text, 140)`
  (statement capture), and with a realistic spread of other users' cards it is planned through
  `ix_tracking_application_user_id_stage_changed_at` with no sequential scan of
  `tracking_application` (`EXPLAIN`).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import BaseCvLabel
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import (
    FetchedPosting,
    JobPostingId,
    JobPostingText,
    PostingTitle,
    SourceUrl,
)
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoredCv
from tailorcraft.domain.tracking.board import ApplicationBoard
from tailorcraft.domain.tracking.value_objects import (
    ApplicationStage,
    ApplicationTitle,
    TrackedApplicationId,
    TrackedRunRef,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.persistence.queries.application_board import (
    SqlAlchemyApplicationBoardQuery,
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
from tailorcraft.infrastructure.persistence.repositories.tracking.tracked_application import (
    SqlAlchemyTrackedApplicationRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.integration.owners import extracted_cv, pasted_posting, succeeded_run
from tests.integration.persistence.owner_rows import persist_user
from tests.integration.tracking.support import a_card

_INDEX = "ix_tracking_application_user_id_stage_changed_at"
_URL = SourceUrl("https://boards.example.com/jobs/4821")
_TITLE = PostingTitle("Senior Python Engineer")


class _Rig:
    def __init__(self, session: AsyncSession, clock: FixedClock) -> None:
        self.session = session
        self.clock = clock
        self.cards = SqlAlchemyTrackedApplicationRepository(session)
        self.runs = SqlAlchemyTailoringRunRepository(session)
        self.query = SqlAlchemyApplicationBoardQuery(session)

    async def board(self, user_id: UserId) -> ApplicationBoard:
        return await self.query.board_for_user(user_id)

    async def run(
        self,
        owner: UserOwner,
        *,
        cv: BaseCv | None = None,
        posting: JobPosting | None = None,
        at_offset: int = 0,
    ) -> TailoringRun:
        """A succeeded run over a real posting (and CV, when given)."""
        at = self.clock.now() + timedelta(seconds=at_offset)
        the_posting = posting or pasted_posting(owner, at)
        if posting is None:
            await SqlAlchemyJobPostingRepository(self.session).add(the_posting)
        ids: dict[str, object] = {"job_posting_id": the_posting.id}
        if cv is not None:
            ids["base_cv_id"] = cv.id
        run = succeeded_run(owner, at, **ids)
        await self.runs.add(run)
        await self.session.flush()
        return run

    async def card(
        self,
        owner: UserOwner,
        run: TailoringRun,
        *,
        stage: ApplicationStage = ApplicationStage.TO_APPLY,
        title: ApplicationTitle | None = None,
        at_offset: int = 0,
        card_id: TrackedApplicationId | None = None,
    ) -> TrackedApplicationId:
        card = a_card(
            owner.user_id,
            self.clock.now() + timedelta(seconds=at_offset),
            stage=stage,
            title=title,
            run_id=run.id,
            card_id=card_id if card_id is not None else self.cards.next_identity(),
        )
        card_id_value = card.id
        await self.cards.add(card)
        await self.session.flush()
        return card_id_value

    async def raw_card(
        self, user_id: UserId, run_id: UUID, *, title: str | None = None
    ) -> TrackedApplicationId:
        """A card over a run row that does not exist — `add` refuses one, so only raw SQL builds it."""
        card_id = TrackedApplicationId(uuid7())
        at = self.clock.now()
        await self.session.execute(
            text(
                "INSERT INTO tracking_application "
                "(id, user_id, tailoring_run_id, stage, title, tracked_at, stage_changed_at, version) "
                "VALUES (:i, :u, :r, 'applied', :t, :a, :a, 1)"
            ),
            {"i": card_id.value, "u": user_id.value, "r": run_id, "t": title, "a": at},
        )
        return card_id


# --- the card, its run, posting and CV -----------------------------------------------------------


async def test_a_card_carries_its_own_fields_and_what_its_joins_found(
    session: AsyncSession, clock: FixedClock
) -> None:
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    cv = extracted_cv(user, clock.now())
    cv.rename(BaseCvLabel("Backend roles"), clock.now())
    await SqlAlchemyBaseCvRepository(session).add(cv)
    posting = JobPosting.from_fetched_url(
        id=JobPostingId(uuid7()),
        owner=user,
        url=_URL,
        fetched=FetchedPosting(text=JobPostingText("Build things. " * 30), title=_TITLE),
        created_at=clock.now(),
    )
    await SqlAlchemyJobPostingRepository(session).add(posting)
    run = await rig.run(user, cv=cv, posting=posting)
    card_id = await rig.card(
        user, run, stage=ApplicationStage.INTERVIEWING, title=ApplicationTitle("Acme — Staff")
    )

    (card,) = (await rig.board(user.user_id)).cards

    assert card.id == card_id
    assert card.tailoring_run_id == TrackedRunRef(run.id.value)
    assert card.stage is ApplicationStage.INTERVIEWING
    assert card.title == "Acme — Staff"
    assert card.tracked_at == clock.now()
    assert card.stage_changed_at == clock.now()
    assert card.version == 1
    assert card.run is not None
    assert card.run.tailoring_run_id == TrackedRunRef(run.id.value)
    assert card.run.requested_at == run.requested_at
    assert card.run.edited is False
    assert card.posting is not None
    assert card.posting.job_posting_id == posting.id.value
    assert card.posting.source == "fetched"
    assert card.posting.title == "Senior Python Engineer"
    assert card.posting.source_url == str(_URL.value)
    assert card.posting.preview == posting.text.value[:140]
    assert card.base_cv is not None
    assert (card.base_cv.base_cv_id, card.base_cv.label, card.base_cv.original_filename) == (
        cv.id.value,
        "Backend roles",
        "cv.pdf",
    )


async def test_a_card_without_a_title_is_listed_with_a_none_title(
    session: AsyncSession, clock: FixedClock
) -> None:
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    await rig.card(user, await rig.run(user), title=None)

    (card,) = (await rig.board(user.user_id)).cards

    assert card.title is None
    assert card.posting is not None
    assert card.posting.source == "pasted"
    assert (card.posting.title, card.posting.source_url) == (None, None)


async def test_a_run_the_user_edited_reads_as_edited(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The positive control for `edited is False` above: a revision turns it on."""
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    run = await rig.run(user)
    await rig.card(user, run)
    stored = await rig.runs.get(run.id)
    stored.revise_cv(TailoredCv("e" * 500), expected_version=stored.version, at=clock.now())
    await session.flush()

    (card,) = (await rig.board(user.user_id)).cards

    assert card.run is not None
    assert card.run.edited is True


async def test_the_preview_is_140_code_points_of_multibyte_text(
    session: AsyncSession, clock: FixedClock
) -> None:
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    body = JobPostingText("Разработчик😀бэкенда " * 20)
    posting = JobPosting.from_pasted_text(
        id=JobPostingId(uuid7()), owner=user, text=body, created_at=clock.now()
    )
    await SqlAlchemyJobPostingRepository(session).add(posting)
    await rig.card(user, await rig.run(user, posting=posting))

    (card,) = (await rig.board(user.user_id)).cards

    assert card.posting is not None
    assert card.posting.preview == body.value[:140]
    assert len(card.posting.preview) == 140
    assert len(card.posting.preview.encode()) > 140


# --- scope and order -----------------------------------------------------------------------------


async def test_only_the_users_own_cards_are_listed(
    session: AsyncSession, clock: FixedClock
) -> None:
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    other = await persist_user(session, clock)
    mine = await rig.card(user, await rig.run(user))
    await rig.card(other, await rig.run(other))

    board = await rig.board(user.user_id)

    assert [card.id for card in board.cards] == [mine]


async def test_a_user_with_no_cards_has_an_empty_board(
    session: AsyncSession, clock: FixedClock
) -> None:
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    other = await persist_user(session, clock)
    await rig.card(other, await rig.run(other))  # the table is not empty, only this user's part

    board = await rig.board(user.user_id)

    assert board == ApplicationBoard(cards=())


async def test_cards_are_ordered_by_stage_change_newest_first(
    session: AsyncSession, clock: FixedClock
) -> None:
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    oldest = await rig.card(user, await rig.run(user), at_offset=0)
    newest = await rig.card(user, await rig.run(user), at_offset=120)
    middle = await rig.card(user, await rig.run(user), at_offset=60)

    board = await rig.board(user.user_id)

    assert [card.id for card in board.cards] == [newest, middle, oldest]


async def test_a_same_second_tie_is_ordered_by_id_descending_in_postgres_uuid_order(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The `Clock` is whole-second, so a tie is ordinary; `id DESC` is the tiebreak, in PostgreSQL's
    `uuid` order (byte order). Ids are fixed so Python and PostgreSQL cannot both be wrong the
    same way."""
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    ids = [
        TrackedApplicationId(UUID("00000000-0000-7000-8000-00000000000a")),
        TrackedApplicationId(UUID("00000000-0000-7000-8000-00000000000c")),
        TrackedApplicationId(UUID("00000000-0000-7000-8000-00000000000b")),
    ]
    for card_id in ids:
        await rig.card(user, await rig.run(user), card_id=card_id)

    board = await rig.board(user.user_id)

    assert [card.id.value for card in board.cards] == sorted(
        (card_id.value for card_id in ids), reverse=True
    )


async def test_order_follows_the_stage_change_not_the_stage(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The client groups by stage and keeps this order, so the query orders across stages."""
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    older_offer = await rig.card(
        user, await rig.run(user), stage=ApplicationStage.OFFER, at_offset=0
    )
    newer_applied = await rig.card(
        user, await rig.run(user), stage=ApplicationStage.APPLIED, at_offset=30
    )

    board = await rig.board(user.user_id)

    assert [card.id for card in board.cards] == [newer_applied, older_offer]


# --- the derived and the dangling ----------------------------------------------------------------


async def test_a_deleted_saved_cv_reads_as_none_and_the_card_is_still_listed(
    session: AsyncSession, clock: FixedClock
) -> None:
    """ADR-0024 decision 3: the reference dangles on purpose; "CV deleted" is derived here."""
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = extracted_cv(user, clock.now())
    await cvs.add(cv)
    run = await rig.run(user, cv=cv)
    card_id = await rig.card(user, run)
    await cvs.remove(cv.id, user)
    await session.flush()

    (card,) = (await rig.board(user.user_id)).cards

    assert card.id == card_id
    assert card.base_cv is None
    assert card.run is not None
    assert card.posting is not None


async def test_another_users_cv_and_posting_at_a_dangling_id_are_not_lent(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The run names ids of rows another user owns (impossible through the use cases, reachable
    through a dangling reference). The owner is in each join condition, so neither is lent."""
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    stranger = await persist_user(session, clock)
    their_cv = extracted_cv(stranger, clock.now())
    their_cv.rename(BaseCvLabel("Not yours"), clock.now())
    await SqlAlchemyBaseCvRepository(session).add(their_cv)
    their_posting = pasted_posting(stranger, clock.now())
    await SqlAlchemyJobPostingRepository(session).add(their_posting)
    run = await rig.run(user, cv=their_cv, posting=their_posting)
    await rig.card(user, run)

    (card,) = (await rig.board(user.user_id)).cards

    assert card.run is not None, "the run is the user's own and is still listed"
    assert card.base_cv is None
    assert card.posting is None


async def test_a_card_over_another_users_run_is_listed_with_nothing_lent(
    session: AsyncSession, clock: FixedClock
) -> None:
    """The run join carries the owner too: a card whose run id names a **stranger's** run (raw SQL
    only) borrows none of its dates, posting or CV, and is still listed so it can be removed."""
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    stranger = await persist_user(session, clock)
    their_run = await rig.run(stranger)
    card_id = await rig.raw_card(user.user_id, their_run.id.value)

    (card,) = (await rig.board(user.user_id)).cards

    assert card.id == card_id
    assert (card.run, card.posting, card.base_cv) == (None, None, None)


async def test_a_card_whose_run_is_missing_is_listed_removable_and_logged_with_its_id_only(
    settings: Settings, session: AsyncSession, clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    """T-36: prevented by the two locks of plan §0.7 and listed anyway, so its owner can remove it.
    One warning names the card; the title (user text) is never in it."""
    configure_logging(settings)
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    secret_title = "PLANTED-TITLE-MARKER"
    orphan = await rig.raw_card(user.user_id, uuid4(), title=secret_title)

    with caplog.at_level(logging.WARNING):
        (card,) = (await rig.board(user.user_id)).cards

    assert card.id == orphan
    assert card.title == secret_title, "the card's own fields are still shown"
    assert (card.run, card.posting, card.base_cv) == (None, None, None)
    records = [r for r in caplog.records if "tracking.board_run_missing" in r.getMessage()]
    assert len(records) == 1
    payload = json.loads(records[0].getMessage())
    assert payload["tracked_application_id"] == str(orphan.value)
    assert set(payload) - {"event", "level", "logger", "timestamp"} == {"tracked_application_id"}
    assert secret_title not in caplog.text


async def test_a_card_with_its_run_present_logs_no_missing_run_warning(
    settings: Settings, session: AsyncSession, clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    """The control for the warning above: it fires for the missing run and only for it."""
    configure_logging(settings)
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    await rig.card(user, await rig.run(user))

    with caplog.at_level(logging.WARNING):
        await rig.board(user.user_id)

    assert not [r for r in caplog.records if "tracking.board_run_missing" in r.getMessage()]


# --- AC-31: no document column is selected -------------------------------------------------------

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


async def test_the_board_statement_selects_no_document_column_and_no_posting_text_beyond_the_preview(
    session: AsyncSession, clock: FixedClock
) -> None:
    """AC-31 by statement capture, over a board whose run is **edited** and whose CV and posting are
    real — so every table the statement could leak from has a row to leak.

    **Observed red, 2026-10-05**, with `_r.c.tailored_cv` added to the adapter's select list
    (restored byte-exact after): `AssertionError: assert ['tailored_cv'] == []`."""
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    cv = extracted_cv(user, clock.now())
    await SqlAlchemyBaseCvRepository(session).add(cv)
    run = await rig.run(user, cv=cv)
    await rig.card(user, run)
    stored = await rig.runs.get(run.id)
    stored.revise_cv(TailoredCv("e" * 500), expected_version=stored.version, at=clock.now())
    await session.flush()

    with _captured_statements(session) as statements:
        board = await rig.board(user.user_id)

    assert board.cards[0].run is not None
    assert board.cards[0].run.edited is True, "the statement had an edited run to describe"
    (statement,) = [s for s in statements if "tracking_application" in s]
    select_list = statement.split(" FROM ", 1)[0]
    assert _DOCUMENT_COLUMNS.findall(select_list) == []
    assert select_list.count("p.text") == select_list.count("left(p.text")
    assert "left(p.text" in select_list, "the preview is cut in SQL"


# --- AC-31: the index is used --------------------------------------------------------------------


async def test_the_board_statement_is_planned_through_the_user_index_without_a_seq_scan(
    session: AsyncSession, clock: FixedClock
) -> None:
    """A spread that makes the index the sensible plan on its own: 60 users with 40 cards each, the
    board's user one of them (1.7 % of the table), then `ANALYZE`. The statement is read off the
    driver, replayed under `EXPLAIN`, and must read `ix_tracking_application_user_id_stage_changed_at`
    and never `Seq Scan on tracking_application`. (`enable_seqscan` is **not** switched off: the
    planner's own choice is the claim, which a disabled-cost plan could not make.)

    **Observed red, 2026-10-05**, with `DROP INDEX ix_tracking_application_user_id_stage_changed_at`
    issued in this test's rolled-back transaction before the `ANALYZE`: the plan became a `Sort`
    (`Sort Key: a.stage_changed_at DESC, a.id DESC`) over a scan and `_INDEX in text_plan` failed."""
    rig = _Rig(session, clock)
    user = await persist_user(session, clock)
    await rig.card(user, await rig.run(user))
    await session.execute(
        text(
            "INSERT INTO identity_user (id, email, password_hash, created_at, password_updated_at) "
            "SELECT gen_random_uuid(), 'spread-' || g || '@example.com', 'x', :t, :t "
            "FROM generate_series(1, 59) AS g"
        ),
        {"t": clock.now()},
    )
    await session.execute(
        text(
            "INSERT INTO tracking_application "
            "(id, user_id, tailoring_run_id, stage, title, tracked_at, stage_changed_at, version) "
            "SELECT gen_random_uuid(), u.id, gen_random_uuid(), 'applied', NULL, :t, :t, 1 "
            "FROM identity_user u, generate_series(1, 40) AS g WHERE u.email LIKE 'spread-%'"
        ),
        {"t": clock.now()},
    )
    await session.execute(text("ANALYZE tracking_application"))
    connection = await session.connection()
    captured: list[tuple[str, object]] = []

    def capture(
        conn: Connection, cursor: object, statement: str, parameters: object, *_: object
    ) -> None:
        if "tracking_application" in statement and statement.lstrip().upper().startswith("SELECT"):
            captured.append((statement, parameters))

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        await rig.board(user.user_id)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(captured) == 1
    statement, parameters = captured[0]
    plan = await connection.exec_driver_sql(f"EXPLAIN {statement}", parameters)  # type: ignore[arg-type]
    text_plan = "\n".join(row[0] for row in plan)
    assert _INDEX in text_plan, text_plan
    assert "Seq Scan on tracking_application" not in text_plan, text_plan
