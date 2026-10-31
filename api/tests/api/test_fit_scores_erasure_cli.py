"""`erase-account` and the fit scores (slice 5.1, T23; AC-36).

The rows go by the FK cascade, and both lines of the command append `; scoring: N fit score(s)`
**after** 2.2's, 2.3's and 3.1's text, which stays byte-identical: the whole line is pinned, so an
inserted segment or a reworded one is red, not only a missing tail.

Rows are committed through the session-scoped engine (`erase_account` opens its own and cannot see a
rolled-back test transaction); the user is deleted at teardown.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.retention import erase_account_command
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import assert_test_database
from tests.integration.persistence.fit_score_rows import insert_score_row
from tests.integration.tracking.support import seed_user


@pytest.fixture(autouse=True)
def _configured_logging(settings: Settings) -> None:
    configure_logging(settings)


async def _account_with_scores(engine: AsyncEngine, clock: FixedClock, *, scores: int) -> UUID:
    async with async_sessionmaker(engine, expire_on_commit=False)() as seeding:
        user_id = await seed_user(seeding, clock, f"qa-erase-scores-{uuid4().hex}@example.com")
        for pair in range(1, scores + 1):
            await insert_score_row(seeding, user_id=user_id.value, pair=pair, scored_at=clock.now())
        await seeding.commit()
    return user_id.value


async def _scores_of(engine: AsyncEngine, user_id: UUID) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM scoring_fit_score WHERE user_id = :u"),
                    {"u": user_id},
                )
            ).scalar_one()
        )


async def _drop_user(engine: AsyncEngine, user_id: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id})


async def test_ac36_the_dry_run_appends_the_scoring_segment_to_the_unchanged_line_and_deletes_nothing(
    settings: Settings,
    engine: AsyncEngine,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert_test_database(settings)
    user_id = await _account_with_scores(engine, clock, scores=2)
    try:
        exit_code = await erase_account_command.erase_account(
            settings, user_id=UserId(user_id), dry_run=True
        )

        assert exit_code == erase_account_command.EXIT_OK
        assert capsys.readouterr().out.strip() == (
            f"would erase account {user_id}: 0 saved CV(s), 0 file(s), 0 login(s) (dry run); "
            "history: 0 tailoring run(s), 0 job posting(s), 0 export job(s); "
            "tracking: 0 tracked application(s); scoring: 2 fit score(s)"
        )
        assert await _scores_of(engine, user_id) == 2, "a dry run deletes nothing"
    finally:
        await _drop_user(engine, user_id)


async def test_ac36_erasure_removes_the_scores_and_appends_the_segment_to_the_unchanged_line(
    settings: Settings,
    engine: AsyncEngine,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert_test_database(settings)
    user_id = await _account_with_scores(engine, clock, scores=3)
    bystander = await _account_with_scores(engine, clock, scores=1)
    try:
        with caplog.at_level(logging.INFO):
            exit_code = await erase_account_command.erase_account(
                settings, user_id=UserId(user_id), dry_run=False
            )

        assert exit_code == erase_account_command.EXIT_OK, caplog.text
        assert capsys.readouterr().out.strip() == (
            f"erased account {user_id}: 0 saved CV(s), 0 file(s) unlinked, 0 failed; "
            "history: 0 tailoring run(s), 0 job posting(s), 0 export job(s), 0 file(s) in all; "
            "tracking: 0 tracked application(s); scoring: 3 fit score(s)"
        )
        assert await _scores_of(engine, user_id) == 0
        assert await _scores_of(engine, bystander) == 1, "another account's score stays"
    finally:
        await _drop_user(engine, user_id)
        await _drop_user(engine, bystander)


async def test_ac36_an_account_with_no_scores_reports_zero(
    settings: Settings,
    engine: AsyncEngine,
    clock: FixedClock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert_test_database(settings)
    user_id = await _account_with_scores(engine, clock, scores=0)
    try:
        await erase_account_command.erase_account(settings, user_id=UserId(user_id), dry_run=True)

        assert capsys.readouterr().out.strip().endswith("; scoring: 0 fit score(s)")
    finally:
        await _drop_user(engine, user_id)
