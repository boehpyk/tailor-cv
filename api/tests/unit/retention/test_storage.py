"""Slice 4.4 (ADR-0035), AC-3: the storage overview's value objects. Pure; no I/O."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tailorcraft.domain.retention.storage import StorageBucket, StorageOverview
from tailorcraft.domain.shared.errors import InvariantViolated


def test_bucket_accepts_zero() -> None:
    bucket = StorageBucket(files=0, bytes=0)
    assert (bucket.files, bucket.bytes) == (0, 0)


def test_bucket_refuses_a_negative_file_count() -> None:
    with pytest.raises(InvariantViolated):
        StorageBucket(files=-1, bytes=0)


def test_bucket_refuses_a_negative_byte_count() -> None:
    with pytest.raises(InvariantViolated):
        StorageBucket(files=0, bytes=-1)


def test_buckets_add_files_and_bytes_separately() -> None:
    assert StorageBucket(files=2, bytes=100) + StorageBucket(files=3, bytes=7) == StorageBucket(
        files=5, bytes=107
    )


def test_adding_the_empty_bucket_changes_nothing() -> None:
    bucket = StorageBucket(files=4, bytes=9)
    assert bucket + StorageBucket(files=0, bytes=0) == bucket


def _overview(scanned_at: datetime) -> StorageOverview:
    empty = StorageBucket(files=0, bytes=0)
    return StorageOverview(
        guest=empty,
        user=empty,
        unreferenced=empty,
        partial=empty,
        unrecognized=empty,
        scanned_at=scanned_at,
    )


def test_overview_accepts_an_aware_scan_instant() -> None:
    at = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)
    assert _overview(at).scanned_at == at


def test_overview_refuses_a_naive_scan_instant() -> None:
    with pytest.raises(InvariantViolated):
        _overview(datetime(2026, 10, 13, 12, 0, 0))
