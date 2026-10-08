"""AC-28 (slice 3.2, T20, test-after) — a PDF's layout changes nothing about how its file is kept or
reclaimed.

Every path that finds export files does it from `(job id, format)` (ADR-0016), which a layout does not
touch. Each test seeds **one ready PDF per layout plus one DOCX** (four rows, four files) on a real
`tmp_path` volume and runs the *real* entry point:

  (a) the guest purge, (b) deleting a history entry, (c) account erasure and `erase-account --dry-run`,
  (d) the orphan sweep over a kept user's files, (e) the claim.

**The absence half** (an unchanged retention and claim module set) is pinned by a source scan, not by
`git diff`: no test in this suite shells out to git, and the api container cannot see `.git`. The scan
asserts none of those modules mentions `layout_template`, which is the property the empty diff stands
for. The orchestrator also runs `git diff --stat bfb98a9 -- <paths>` by hand.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId, LayoutTemplate
from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.retention import erase_account_command
from tailorcraft.infrastructure.settings import Settings
from tests.api.claim_race_world import World
from tests.api.claim_retention_support import (
    age_on_disk,
    orphan_sweep,
    purge_plus_30_days,
    snapshot_files,
)
from tests.api.me_support import ME_RUNS, Entry, new_client, seed_entry
from tests.integration.owners import queued_export

CLAIM_URL = "/api/me/guest-work/claim"
LAYOUTS = [LayoutTemplate.CLASSIC, LayoutTemplate.MODERN, LayoutTemplate.FORMAL]


@pytest.fixture(autouse=True)
def _reset_redis(clear_redis: None) -> None:
    """The limiters and the purge lock live in Redis, which no rollback reaches."""


@pytest_asyncio.fixture
async def world(
    settings: Settings, engine: AsyncEngine, password_hasher: object, tmp_path: Path
) -> AsyncIterator[World]:
    w = World(settings, engine, password_hasher, tmp_path)
    try:
        yield w
    finally:
        await w.cleanup()


def _ready(
    owner: Owner, entry: Entry, at: datetime, fmt: ExportFormat, layout: LayoutTemplate | None
) -> ExportJob:
    job = ExportJob.request(
        id=ExportJobId(value=uuid4()),
        owner=owner,
        tailoring_run_id=entry.run_id,
        document=queued_export(owner, entry.run, at).document,
        format=fmt,
        layout_template=layout,
        run_version=entry.run_version,
        requested_at=at,
    )
    job.mark_started(at)
    job.mark_ready(byte_size=10, render_duration_ms=10, at=at)
    job.release_events()
    return job


async def seed_four(
    world: World, owner: Owner, *, at: datetime | None = None
) -> tuple[Entry, list[FileRef]]:
    """A history entry whose three PDFs (one per layout) and one DOCX are ready, bytes on disk."""
    at = at or datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    async with async_sessionmaker(world.engine, expire_on_commit=False)() as session:
        entry = await seed_entry(session, world.settings, owner, at=at, ready_formats=())
        refs: list[FileRef] = []
        for fmt, layout in [(ExportFormat.PDF, x) for x in LAYOUTS] + [(ExportFormat.DOCX, None)]:
            job = _ready(owner, entry, at, fmt, layout)
            await SqlAlchemyExportJobRepository(session).add(job)
            (world.root / job.storage_ref.key).parent.mkdir(parents=True, exist_ok=True)
            (world.root / job.storage_ref.key).write_bytes(
                f"{fmt.value} {layout} {job.id.value}".encode()
            )
            (world.root / job.storage_ref.key).chmod(0o600)
            refs.append(job.storage_ref)
        await session.commit()
    return entry, refs


async def _jobs(engine: AsyncEngine, run_id: object) -> dict[UUID, tuple[str, str | None, str]]:
    """`{job id: (format, layout_template, owner column text)}` — read on a fresh connection."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, format, layout_template, coalesce(user_id::text, guest_session_id::text)"
                " FROM export_job WHERE tailoring_run_id = :r"
            ),
            {"r": getattr(run_id, "value", run_id)},
        )
        return {r[0]: (str(r[1]), r[2], str(r[3])) for r in rows.all()}


def _present(root: Path, refs: list[FileRef]) -> list[bool]:
    return [(root / r.key).exists() for r in refs]


async def test_the_guest_purge_deletes_every_row_and_file_whatever_the_layout(world: World) -> None:
    async with new_client(world.app) as browser:
        guest = await world.mint(browser)
    entry, refs = await seed_four(world, guest)
    assert len(await _jobs(world.engine, entry.run_id)) == 4
    assert _present(world.root, refs) == [True] * 4, "positive control: the files were seeded"

    assert await purge_plus_30_days(world.settings, pytest.MonkeyPatch()) == 0

    assert await _jobs(world.engine, entry.run_id) == {}
    assert _present(world.root, refs) == [False] * 4


async def test_deleting_a_history_entry_deletes_all_four_rows_and_files(world: World) -> None:
    account = await world.account()
    entry, refs = await seed_four(world, account.owner)
    assert len(await _jobs(world.engine, entry.run_id)) == 4

    async with new_client(world.app) as client:
        response = await client.delete(f"{ME_RUNS}/{entry.run_id.value}", headers=account.headers)

    assert response.status_code == 204, response.text
    assert await _jobs(world.engine, entry.run_id) == {}
    assert _present(world.root, refs) == [False] * 4


async def test_erase_account_dry_run_counts_all_four_and_a_real_run_removes_them(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    account = await world.account()
    entry, refs = await seed_four(world, account.owner)

    dry = await erase_account_command.erase_account(
        world.settings, user_id=account.user_id, dry_run=True
    )

    assert dry == erase_account_command.EXIT_OK
    out = capsys.readouterr().out
    assert "4 export job(s)" in out
    assert "5 file(s)" in out, "the saved CV's file plus four exports"
    assert len(await _jobs(world.engine, entry.run_id)) == 4, "a dry run deletes nothing"
    assert _present(world.root, refs) == [True] * 4

    real = await erase_account_command.erase_account(
        world.settings, user_id=account.user_id, dry_run=False
    )

    assert real == erase_account_command.EXIT_OK
    assert "4 export job(s)" in capsys.readouterr().out
    assert await _jobs(world.engine, entry.run_id) == {}
    assert _present(world.root, refs) == [False] * 4


async def test_the_orphan_sweep_reclaims_none_of_a_kept_users_files_but_does_reclaim_a_true_orphan(
    world: World,
) -> None:
    account = await world.account()
    entry, refs = await seed_four(world, account.owner)
    orphan = FileRef.for_export(ExportJobId(value=uuid4()), ExportFormat.PDF)
    (world.root / orphan.key).parent.mkdir(parents=True, exist_ok=True)
    (world.root / orphan.key).write_bytes(b"no row points here")
    age_on_disk(world.root, *refs, orphan)
    before = snapshot_files(world.root, *refs)

    assert await orphan_sweep(world.settings) == 0

    assert not (world.root / orphan.key).exists(), "positive control: the sweep really ran"
    assert snapshot_files(world.root, *refs) == before
    assert len(await _jobs(world.engine, entry.run_id)) == 4


async def test_a_claim_moves_all_four_rows_with_their_layouts_and_touches_no_file(
    world: World,
) -> None:
    account = await world.account()
    async with new_client(world.app) as browser:
        guest = await world.mint(browser)
        entry, refs = await seed_four(world, guest)
        before_rows = await _jobs(world.engine, entry.run_id)
        before_files = snapshot_files(world.root, *refs)
        assert sorted(v[1] or "" for v in before_rows.values()) == [
            "",
            "classic",
            "formal",
            "modern",
        ]

        claimed = await browser.post(CLAIM_URL, headers=account.headers)

    assert claimed.status_code == 200, claimed.text
    after = await _jobs(world.engine, entry.run_id)
    assert after.keys() == before_rows.keys(), "no id changes"
    assert {k: v[:2] for k, v in after.items()} == {k: v[:2] for k, v in before_rows.items()}
    assert {v[2] for v in after.values()} == {str(account.user_id.value)}, (
        "all four now belong to the user"
    )
    assert snapshot_files(world.root, *refs) == before_files


_SRC = Path(__file__).resolve().parents[3] / "src" / "tailorcraft"
_RETENTION_AND_CLAIM = [
    *sorted((_SRC / "domain" / "retention").rglob("*.py")),
    *sorted((_SRC / "application" / "retention").rglob("*.py")),
    *sorted((_SRC / "infrastructure" / "retention").rglob("*.py")),
    *sorted((_SRC / "infrastructure" / "persistence" / "retention").rglob("*.py")),
    _SRC / "infrastructure" / "identity" / "claim_access.py",
]


def test_no_retention_or_claim_module_mentions_a_layout() -> None:
    assert len(_RETENTION_AND_CLAIM) > 10, "the scan must see the modules, or it passes on nothing"
    assert all(p.is_file() for p in _RETENTION_AND_CLAIM)
    mentions = [p.name for p in _RETENTION_AND_CLAIM if "layout_template" in p.read_text()]
    assert mentions == []
