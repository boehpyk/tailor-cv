"""AC-21 / AC-22 — a claimed session's work survives the guest purge and the orphan sweep (slice
2.4, T24, **[proof]**).

**Tier: proof.** The purge and the sweep are not edited by this slice; the claim only changes who
owns a row. These tests are observed red under the named mutations (below), the source restored
byte-exact (`git diff -- api/src` empty) and re-observed green. Everything is committed over
`build_concurrent_app` (a real session per request), the volume is a per-test `tmp_path`, and the
purge and the sweep run through `purge_command` — the real entry points — so the rows they judge are
the rows the claim wrote.

- **AC-21.** Guest work (a CV the guest uploaded, a seeded CV / posting / succeeded run / ready PDF) is
  claimed over HTTP. A *planted expired guest* with its own work is the positive control: the purge,
  with its clock 30 days ahead, must take that guest's rows and files **and** leave every row the user
  owns byte-identical (`SELECT t::text` per table, read back on a fresh connection) and every claimed
  file present, byte-identical, mode `0600`. Asserted by table reads and `stat`, never by the report.
- **AC-22.** The same data plus a **working copy** (a guest CV with `copied_from_base_cv_id`) whose
  unlink is made to fail during the claim, so its file survives the claim as an orphan. Every file is
  aged 60 h with `os.utime` and a planted true orphan sits beside them. The real sweep reclaims the
  orphan and the working copy's file and **none** of the claimed files.

**Mutation record (T24)** — applied to `src/` by hand, restored byte-exact:
- The `export_job` `UPDATE` dropped from `SqlAlchemyGuestWorkClaim.transfer` (its `WHERE` made to match
  no row): 2 failed. AC-21 red, `AssertionError: after the claim the user owns no export_job row` —
  the export rows are cascaded away with the session at the claim's own `DELETE`, i.e. before the
  purge is even asked, which is exactly the outcome the spec names ("cascaded with the session").
  AC-22 red, `AssertionError: the sweep reclaimed a CLAIMED file (6a/21/….pdf)` — the export file has no
  row, so the sweep takes it. Restored with `git checkout -- api/src`; `git diff -- api/src` empty;
  2 passed.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
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
from tests.api.me_support import Entry, new_client
from tests.integration.claim_race_support import (
    OWNED_TABLES,
    SeededGuest,
    owned_row_counts,
    seed_guest_with_work,
    session_exists,
)
from tests.integration.owners import extracted_cv

CLAIM_URL = "/api/me/guest-work/claim"


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """The register and claim limiters and the purge lock live in Redis, which no rollback reaches."""


@pytest_asyncio.fixture
async def world(
    settings: Settings, engine: AsyncEngine, password_hasher: object, tmp_path: Path
) -> AsyncIterator[World]:
    w = World(settings, engine, password_hasher, tmp_path)
    try:
        yield w
    finally:
        await w.cleanup()


async def _file_key(engine: AsyncEngine, cv_id: object) -> FileRef:
    async with engine.connect() as conn:
        key = (
            await conn.execute(
                text("SELECT file_key FROM intake_base_cv WHERE id = :i"), {"i": cv_id}
            )
        ).scalar_one()
    return FileRef(key=str(key))


async def _plant_expired_guest(world: World, files: LocalFileStore) -> SeededGuest:
    """The positive control: a guest session long expired, owning a CV, a posting, a run and an
    export whose files are on the volume. Cleaned up by `drop_rows` whatever the purge did."""
    started = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=72)
    planted = await seed_guest_with_work(world.engine, files, started_at=started)
    world.guests.append(planted.guest.value)
    return planted


async def _put_missing_cv_files(root: Path, keys: list[str]) -> None:
    """`seed_entry` writes the export's bytes but not the seeded CV's; a claimed CV with no file would
    make "the file survives" vacuous for it, so give every CV key real bytes."""
    for key in keys:
        if not (root / key).exists():
            await LocalFileStore(root).put(FileRef(key=key), f"%PDF-1.4 cv {key}".encode())


async def _claim_a_guest_with_work(world: World) -> tuple[Entry, object, list[FileRef]]:
    """A registered account claims a live guest's work over HTTP; returns the entry, the account and
    every file the claim handed over (the uploaded CV's, the seeded CV's, the export's)."""
    account = await world.account()
    async with new_client(world.app) as browser:
        guest, entry, _ = await world.guest_with_work(browser)
        async with world.engine.connect() as conn:
            keys = [
                str(k)
                for (k,) in (
                    await conn.execute(
                        text("SELECT file_key FROM intake_base_cv WHERE guest_session_id = :g"),
                        {"g": guest.guest_session_id.value},
                    )
                ).all()
            ]
        await _put_missing_cv_files(world.root, keys)
        claimed = await browser.post(CLAIM_URL, headers=account.headers)
        assert claimed.status_code == 200, claimed.text
        assert not await session_exists(world.engine, guest.guest_session_id)
    refs = [FileRef(key=k) for k in keys] + [job.storage_ref for job in entry.jobs]
    return entry, account, refs


async def test_ac21_a_purge_thirty_days_on_leaves_every_claimed_row_and_file_untouched(
    world: World, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim moved a CV, a posting, a run and a ready export to the user. A purge with its clock
    +30 d deletes the planted expired guest (rows and files: the control that the purge really ran
    and really unlinks) and changes **nothing** the user owns: every column of every owned row is
    identical, and every claimed file is present, byte-identical, mode `0600`.

    **Mutation (T24), restored byte-exact:** the `export_job` `UPDATE` dropped from the claim's
    `transfer` -> `AssertionError: after the claim the user owns no export_job row`."""
    files = LocalFileStore(world.root)
    entry, account, refs = await _claim_a_guest_with_work(world)
    user = account.user_id  # type: ignore[attr-defined]
    planted = await _plant_expired_guest(world, files)

    rows_before = await user_rows(world.engine, user)
    files_before = snapshot_files(world.root, *refs)
    for table in OWNED_TABLES:
        assert rows_before[table], f"after the claim the user owns no {table} row"
    assert len(rows_before["export_job"]) == 1
    assert all(mode == 0o600 for _, mode in files_before.values()), files_before.keys()

    exit_code = await purge_plus_30_days(world.settings, monkeypatch)

    assert exit_code == purge_command.EXIT_OK
    # The control: the purge ran, deleted the expired guest, and unlinked its files.
    assert not await session_exists(world.engine, planted.guest)
    assert not any((world.root / ref.key).exists() for ref in planted.files)
    assert await owned_row_counts(world.engine, guest=planted.guest) == dict.fromkeys(
        OWNED_TABLES, 0
    )
    # The claim: byte-identical rows, present identical files.
    assert await user_rows(world.engine, user) == rows_before
    assert snapshot_files(world.root, *refs) == files_before
    assert entry.jobs[0].storage_ref in refs


async def test_ac22_the_orphan_sweep_takes_the_orphan_and_a_dropped_copys_file_never_a_claimed_one(
    world: World,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same data, plus a working copy (a guest CV with `copied_from_base_cv_id`) that the claim
    drops — its unlink is made to fail, so the **row is gone and the file is not**: an orphan. A true
    orphan is planted beside it. Every file is aged 60 h on disk (`os.utime`); the real sweep, on the
    real clock, reclaims exactly those two and every claimed file stays, bytes and mode intact.

    **Mutation (T24), restored byte-exact:** the `export_job` `UPDATE` dropped from the claim's
    `transfer` — the export file then has no row and is reclaimed -> `AssertionError: the sweep
    reclaimed a CLAIMED file`."""
    files = LocalFileStore(world.root)
    account = await world.account()
    async with new_client(world.app) as browser:
        guest, entry, _ = await world.guest_with_work(browser)
        owner = GuestOwner(guest.guest_session_id)
        async with async_sessionmaker(world.engine, expire_on_commit=False)() as session:
            copy = extracted_cv(owner, datetime.now(UTC).replace(microsecond=0))
            await SqlAlchemyBaseCvRepository(session).add(copy)
            await session.commit()
            copy_id = copy.id
            copy_ref = copy.file
        async with world.engine.begin() as conn:
            await conn.execute(
                text("UPDATE intake_base_cv SET copied_from_base_cv_id = :s WHERE id = :i"),
                {"s": uuid4(), "i": copy_id.value},
            )
        await files.put(copy_ref, b"%PDF-1.4 a working copy's bytes")
        async with world.engine.connect() as conn:
            claimed_cv_keys = [
                str(k)
                for (k,) in (
                    await conn.execute(
                        text(
                            "SELECT file_key FROM intake_base_cv "
                            "WHERE guest_session_id = :g AND copied_from_base_cv_id IS NULL"
                        ),
                        {"g": guest.guest_session_id.value},
                    )
                ).all()
            ]

        await _put_missing_cv_files(world.root, claimed_cv_keys)
        real_delete = LocalFileStore.delete

        async def failing_delete(self: LocalFileStore, ref: FileRef) -> None:
            if ref == copy_ref:
                raise OSError("EIO (made to fail by the test)")
            await real_delete(self, ref)

        with monkeypatch.context() as patched, caplog.at_level(logging.WARNING):
            patched.setattr(LocalFileStore, "delete", failing_delete)
            response = await browser.post(CLAIM_URL, headers=account.headers)
        assert response.status_code == 200, response.text
        assert response.json()["working_copies_dropped"] == 1, response.json()

    claimed_refs = [FileRef(key=k) for k in claimed_cv_keys] + [
        job.storage_ref for job in entry.jobs
    ]
    orphan_ref = FileRef(key=f"{uuid4().hex[:2]}/{uuid4().hex[2:4]}/{uuid4()}.pdf")
    await files.put(orphan_ref, b"%PDF-1.4 a true orphan")
    assert (world.root / copy_ref.key).exists(), "the failed unlink must leave the file behind"
    assert len(claimed_refs) == 3  # the uploaded CV, the seeded CV, the export
    age_on_disk(world.root, *claimed_refs, copy_ref, orphan_ref)
    user = account.user_id
    rows_before = await user_rows(world.engine, user)
    files_before = snapshot_files(world.root, *claimed_refs)

    exit_code = await orphan_sweep(world.settings)

    assert exit_code == purge_command.EXIT_OK
    assert not (world.root / orphan_ref.key).exists(), "the true orphan survived the sweep"
    assert not (world.root / copy_ref.key).exists(), "the dropped copy's orphaned file survived"
    for ref in claimed_refs:
        assert (world.root / ref.key).exists(), f"the sweep reclaimed a CLAIMED file ({ref.key})"
    assert snapshot_files(world.root, *claimed_refs) == files_before, "a CLAIMED file was touched"
    assert await user_rows(world.engine, user) == rows_before
