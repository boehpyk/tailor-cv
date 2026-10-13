"""Slice 4.4 (ADR-0035), AC-3: `GuestSessionErasureReport` and `ScannedFile.size_bytes`. Pure.

The report is `PurgeReport`'s file pair and nothing else: there is no separate failure count to
disagree with the detail tuple, so the only invariant it can own is `files_unlinked >= 0`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tailorcraft.domain.retention.value_objects import (
    FileUnlinkFailure,
    GuestSessionErasureReport,
    ScannedFile,
)
from tailorcraft.domain.shared.errors import InvariantViolated


def test_erasure_report_accepts_zero_unlinked_and_no_failures() -> None:
    report = GuestSessionErasureReport(files_unlinked=0, file_unlink_failures=())
    assert report.files_unlinked == 0


def test_erasure_report_carries_its_failures() -> None:
    failure = FileUnlinkFailure(error_type="FileStoreUnavailable")
    report = GuestSessionErasureReport(files_unlinked=2, file_unlink_failures=(failure,))
    assert report.file_unlink_failures == (failure,)


def test_erasure_report_refuses_a_negative_unlinked_count() -> None:
    with pytest.raises(InvariantViolated):
        GuestSessionErasureReport(files_unlinked=-1, file_unlink_failures=())


def _scanned(size_bytes: int) -> ScannedFile:
    return ScannedFile(
        ref=None,
        created_at=datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC),
        is_partial=False,
        size_bytes=size_bytes,
    )


def test_scanned_file_accepts_an_empty_file() -> None:
    assert _scanned(0).size_bytes == 0


def test_scanned_file_refuses_a_negative_size() -> None:
    with pytest.raises(InvariantViolated):
        _scanned(-1)
