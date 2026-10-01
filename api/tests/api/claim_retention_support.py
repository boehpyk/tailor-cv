"""Shared by slice 2.4's retention proofs (T24, AC-21/AC-22) and the upgrade proof (T25, AC-34): the
purge and the orphan sweep run through their **real entry points** (`purge_command`), over committed
rows and a per-test upload volume, and a whole-row snapshot of what a user owns.

**How "+30 days" is staged.** The purge judges expiry by `SystemClock().now()` built inside
`purge_command`, and that module-level name *is* its clock seam: `purge_plus_30_days` swaps it for a
`FixedClock` 30 days ahead, so every guest session in the database is long past its window — the
harshest purge the product can run. The orphan sweep is run on the **real clock** with files aged
60 h on disk by `os.utime` (past window 24 h + grace 24 h): the honest mechanism, since the scanner
judges age by `st_mtime` and nothing else.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import FixedClock, SystemClock
from tailorcraft.infrastructure.retention import purge_command
from tailorcraft.infrastructure.settings import Settings
from tests.integration.claim_race_support import OWNED_TABLES

AGED_HOURS = 60


async def purge_plus_30_days(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> int:
    """One full `purge-guests` run, the clock 30 days ahead. Returns the command's exit code."""
    later = SystemClock().now() + timedelta(days=30)
    with monkeypatch.context() as patched:
        patched.setattr(purge_command, "SystemClock", lambda: FixedClock(later))
        return await purge_command._purge_guests(settings, dry_run=False, limit=None)


async def orphan_sweep(settings: Settings) -> int:
    """One `purge-guests --orphans` run on the real clock (window 24 h, grace 24 h)."""
    return await purge_command._reclaim_orphans(settings, dry_run=False, limit=None, grace_hours=24)


def age_on_disk(root: Path, *refs: FileRef, hours: int = AGED_HOURS) -> None:
    """`os.utime` each file `hours` into the past — past the sweep's window plus grace."""
    old = (SystemClock().now() - timedelta(hours=hours)).timestamp()
    for ref in refs:
        os.utime(root / ref.key, (old, old))


async def user_rows(engine: AsyncEngine, user: UserId) -> dict[str, list[str]]:
    """Every column of every row the user owns, per table, as text, ordered by id — a byte-level
    comparison that needs no per-column knowledge (a new column is covered the day it lands)."""
    out: dict[str, list[str]] = {}
    async with engine.connect() as conn:
        for table in OWNED_TABLES:
            rows = await conn.execute(
                text(f"SELECT t::text FROM {table} t WHERE user_id = :u ORDER BY id"),  # noqa: S608 -- test-owned names
                {"u": user.value},
            )
            out[table] = [str(row[0]) for row in rows.all()]
    return out


def snapshot_files(root: Path, *refs: FileRef) -> dict[str, tuple[bytes, int]]:
    """`{key: (bytes, permission bits)}` — `0600` is asserted by the caller, never assumed."""
    return {
        ref.key: (
            (root / ref.key).read_bytes(),
            (root / ref.key).stat().st_mode & 0o777,
        )
        for ref in refs
    }
