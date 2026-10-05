"""AC-27 — the guest purge and the orphan sweep cannot reach a tracked application (slice 3.1, T24,
**[after]**).

A user with three cards over three runs (everything 30 days old, a ready export file and a CV file per
run on a real per-test volume), beside one expired guest session with its own run and files. A full
purge (its clock 30 days ahead) and an orphan sweep (every file aged 60 h with `os.utime`) must:

- leave every column of every `tracking_application` row byte-identical (`SELECT t::text`, read on a
  separate connection), and the user's run / posting / CV / export rows likewise;
- leave every one of the user's files present, byte-identical, mode `0600` (the sweep reclaims 0);
- take the guest's rows and files (the control that the purge really ran and really unlinks).

Tagged [after], not [proof], on purpose (spec AC-27): the card has no guest column, so no one-line
mutation of the purge reaches it. This is the regression guard that the purge is never widened; AC-16's
schema test is the guard that matters. Counts are scoped to ids this test created.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tracking.value_objects import ApplicationStage
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.retention import purge_command
from tailorcraft.infrastructure.settings import Settings
from tests.api.claim_race_world import World
from tests.api.claim_retention_support import (
    age_on_disk,
    orphan_sweep,
    purge_plus_30_days,
    snapshot_files,
    user_rows,
)
from tests.api.me_support import Entry, seed_entry
from tests.api.tracking_support import Seeded, seed_card
from tests.integration.claim_race_support import (
    OWNED_TABLES,
    owned_row_counts,
    seed_guest_with_work,
    session_exists,
)


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """The purge lock and heartbeat live in Redis, which no rollback reaches."""


@pytest_asyncio.fixture
async def world(
    settings: Settings, engine: AsyncEngine, password_hasher: object, tmp_path: Path
) -> AsyncIterator[World]:
    w = World(settings, engine, password_hasher, tmp_path)
    try:
        yield w
    finally:
        await w.cleanup()


async def _cards(engine: AsyncEngine, user_id: object) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT t::text FROM tracking_application t WHERE user_id = :u ORDER BY id"),
            {"u": user_id},
        )
        return [str(r[0]) for r in rows.all()]


async def _user_with_three_cards(
    world: World,
) -> tuple[object, list[Seeded], list[FileRef]]:
    account = await world.account()
    old = datetime.now(UTC).replace(microsecond=0) - timedelta(days=30)
    stages = (ApplicationStage.TO_APPLY, ApplicationStage.INTERVIEWING, ApplicationStage.OFFER)
    seeded: list[Seeded] = []
    refs: list[FileRef] = []
    files = LocalFileStore(world.root)
    async with async_sessionmaker(world.engine, expire_on_commit=False)() as session:
        for i, stage in enumerate(stages):
            entry: Entry = await seed_entry(session, world.settings, account.owner, at=old)
            seeded.append(
                await seed_card(
                    session,
                    world.settings,
                    account,
                    at=old,
                    stage=stage,
                    title=f"card {i}" if i else None,
                    entry=entry,
                )
            )
            refs.extend(job.storage_ref for job in entry.jobs)
            async with world.engine.connect() as conn:
                key = (
                    await conn.execute(
                        text("SELECT file_key FROM intake_base_cv WHERE id = :i"),
                        {"i": entry.cv_id.value},
                    )
                ).scalar_one()
            cv_ref = FileRef(key=str(key))
            await files.put(cv_ref, f"%PDF-1.4 cv {key}".encode())
            refs.append(cv_ref)
    return account.user_id, seeded, refs


async def test_ac27_purge_and_sweep_beside_cards_leave_every_card_column_and_file_untouched(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = LocalFileStore(world.root)
    user, seeded, refs = await _user_with_three_cards(world)
    expired = await seed_guest_with_work(
        world.engine,
        files,
        started_at=datetime.now(UTC).replace(microsecond=0) - timedelta(hours=72),
    )
    world.guests.append(expired.guest.value)

    cards_before = await _cards(world.engine, user.value)  # type: ignore[attr-defined]
    rows_before = await user_rows(world.engine, user)  # type: ignore[arg-type]
    files_before = snapshot_files(world.root, *refs)
    assert len(seeded) == 3, "the arrangement must seed three cards"
    assert len(cards_before) == 3, "the arrangement must hold three cards"
    assert len(refs) == 6
    for table in OWNED_TABLES:
        assert len(rows_before[table]) == 3, f"expected 3 {table} rows for the user"
    assert all(mode == 0o600 for _, mode in files_before.values()), files_before.keys()
    assert all((world.root / r.key).exists() for r in expired.files), "guest files must exist first"

    purge_exit = await purge_plus_30_days(world.settings, monkeypatch)

    assert purge_exit == purge_command.EXIT_OK
    # The control: the purge ran, took the expired guest, and unlinked its files.
    assert not await session_exists(world.engine, expired.guest)
    assert await owned_row_counts(world.engine, guest=expired.guest) == dict.fromkeys(
        OWNED_TABLES, 0
    )
    assert not any((world.root / r.key).exists() for r in expired.files)
    # The user's side after the purge.
    assert await _cards(world.engine, user.value) == cards_before  # type: ignore[attr-defined]
    assert await user_rows(world.engine, user) == rows_before  # type: ignore[arg-type]
    assert snapshot_files(world.root, *refs) == files_before

    # The sweep: every file aged past window + grace, plus a planted true orphan as its control.
    orphan = FileRef(key="ab/cd/00000000-0000-4000-8000-000000000027.pdf")
    await files.put(orphan, b"%PDF-1.4 a true orphan")
    age_on_disk(world.root, *refs, orphan)
    files_before = snapshot_files(world.root, *refs)

    sweep_exit = await orphan_sweep(world.settings)

    assert sweep_exit == purge_command.EXIT_OK
    assert not (world.root / orphan.key).exists(), "the sweep must have run: the orphan survived"
    for ref in refs:
        assert (world.root / ref.key).exists(), f"the sweep reclaimed a USER file ({ref.key})"
    assert snapshot_files(world.root, *refs) == files_before
    assert await _cards(world.engine, user.value) == cards_before  # type: ignore[attr-defined]
    assert await user_rows(world.engine, user) == rows_before  # type: ignore[arg-type]
