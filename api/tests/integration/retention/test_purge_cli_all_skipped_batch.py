"""AC-42 (slice 2.5, carried from 2.4): the purge CLI keeps going past an all-skipped batch.

Since 2.4 a batch can examine sessions and delete none of them because a user claimed them in
between (`sessions_skipped > 0`). That is not "no progress": the next batch reads a different
backlog. The loop must therefore stop on `deleted + skipped == 0`, never on `deleted == 0` alone.

The use case is replaced by a scripted sequence of reports: the loop's control flow is the unit
under test, and a real database could not be made to produce "skipped 3, then deleted 2" on demand.
`_purge_batches` is driven directly with a stand-in for `_purge_use_case`; no Redis, no database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from tailorcraft.domain.retention.value_objects import PurgeReport
from tailorcraft.infrastructure.retention import purge_command
from tailorcraft.infrastructure.settings import Settings


def _report(*, deleted: int, skipped: int) -> PurgeReport:
    return PurgeReport(
        examined=deleted + skipped,
        sessions_deleted=deleted,
        sessions_skipped=skipped,
        sessions_failed=0,
        session_purge_failures=(),
        files_unlinked=0,
        files_failed=0,
        file_unlink_failures=(),
        duration_ms=0,
        dry_run=False,
    )


class _ScriptedPurge:
    def __init__(self, reports: list[PurgeReport]) -> None:
        self._reports = list(reports)
        self.calls = 0

    async def __call__(self) -> PurgeReport:
        self.calls += 1
        return self._reports.pop(0)


class _Backlog:
    async def count(self) -> int:
        return 0


class _Session:
    async def commit(self) -> None:
        return None


class _Hold:
    def __init__(self) -> None:
        self.refreshes = 0

    async def refresh(self) -> None:
        self.refreshes += 1


async def test_a_batch_that_only_skipped_does_not_end_the_run(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(deleted 0, skipped 3) -> (deleted 2, skipped 0) -> (0, 0): three batches, not one."""
    purge = _ScriptedPurge(
        [
            _report(deleted=0, skipped=3),
            _report(deleted=2, skipped=0),
            _report(deleted=0, skipped=0),
        ]
    )

    @asynccontextmanager
    async def _fake_use_case(*_: Any, **__: Any) -> AsyncIterator[tuple[Any, Any, Any]]:
        yield purge, _Backlog(), _Session()

    monkeypatch.setattr(purge_command, "_purge_use_case", _fake_use_case)
    hold = _Hold()

    run = await purge_command._purge_batches(
        settings,
        limit=None,
        hold=hold,  # type: ignore[arg-type]
    )

    assert run.totals.batches == 3
    assert purge.calls == 3
    assert run.totals.sessions_deleted == 2
    assert run.totals.sessions_skipped == 3
