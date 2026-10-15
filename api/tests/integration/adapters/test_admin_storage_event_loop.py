"""AC-43 (slice 4.4, T26) — the event loop stays free while an admin reads the storage overview and
previews an orphan sweep over a 10 000-file volume.

Both routes walk the whole volume through `LocalOrphanFileScanner` (chunked `asyncio.to_thread`).
The sample is the loop's **turnaround** (`sleep(pace) + GET /health/live`, minus the pace; 1.6's T47
method, `loop_liveness.py`), never the request alone, and the sampler ends the batch: workers loop
until it has its floor, so the work's duration is an output, not a precondition.

Hard bound: the work runs in tasks and is awaited under `wait_for`, so a regression fails the test
rather than hanging it.

**Mutation 11 (2026-10-14)** — the `await asyncio.to_thread(_next_chunk, walker)` in
`scan_older_than` replaced by a bare `_next_chunk(walker)`: p95 healthy 1.82 ms,
mutated 379.87 ms (red). Source restored byte-exact.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.infrastructure.identity.password_hasher import Argon2PasswordHasher
from tailorcraft.infrastructure.settings import Settings
from tests.api.admin_guest_world import World, open_world

from .loop_liveness import SAMPLE_FLOOR, hammer_health_live_until_floor

pytestmark = pytest.mark.slow

_FILES = 10_000
_P95_BUDGET_SECONDS = 0.050
# Floor above the helper's default: the walk blocks (when mutated) from the first instant, and a p95
# over 20 samples is just the max.
_FLOOR = max(SAMPLE_FLOOR, 100)
# Measured p95 of the turnaround (n=100): healthy 1.82 ms (max 223 ms, one outlier); mutation 11
# (walk inline) 379.87 ms (max 424 ms) against the 50 ms budget, i.e. red by 7.6x.


@pytest_asyncio.fixture
async def world(
    settings: Settings, engine: AsyncEngine, password_hasher: Argon2PasswordHasher, tmp_path: Path
) -> AsyncIterator[World]:
    async with open_world(
        settings, engine, password_hasher, tmp_path / "uploads", clean_guest_sessions=True
    ) as w:
        yield w


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


async def test_loop_turnaround_p95_stays_under_50ms_during_storage_overview_and_sweep_preview(
    world: World,
) -> None:
    for _ in range(_FILES):
        name = uuid4()
        shard = world.upload_dir / name.hex[0:2] / name.hex[2:4]
        shard.mkdir(parents=True, exist_ok=True)
        (shard / f"{name}.pdf").write_bytes(b"x")
    admin = await world.account(admin=True)
    enough = asyncio.Event()
    scans = {"storage": 0, "sweep": 0}

    async def work(route: str, name: str) -> None:
        while not enough.is_set():
            response = await admin.client.get(route, headers=admin.headers)
            assert response.status_code == 200, response.text
            scans[name] += 1

    sampler = asyncio.ensure_future(
        hammer_health_live_until_floor(world.http, enough=enough, floor=_FLOOR)
    )
    await asyncio.sleep(0)
    try:
        await asyncio.wait_for(
            asyncio.gather(
                work("/api/admin/storage", "storage"), work("/api/admin/orphan-sweep", "sweep")
            ),
            timeout=120,
        )
    finally:
        enough.set()
    turnarounds = await asyncio.wait_for(sampler, timeout=15)

    assert min(scans.values()) >= 1, scans  # the work really ran
    assert len(turnarounds) >= _FLOOR
    p95 = _p95(turnarounds)
    assert p95 <= _P95_BUDGET_SECONDS, (
        f"/health/live turnaround p95 {p95 * 1000:.2f} ms (n={len(turnarounds)}, "
        f"max={max(turnarounds) * 1000:.2f} ms) during storage overview + sweep preview over "
        f"{_FILES} files — the event loop was blocked"
    )
