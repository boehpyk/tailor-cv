"""UUIDv7 generation (ADR-0007)."""

from __future__ import annotations

import time

from tailorcraft.infrastructure.identifiers import uuid7


def test_is_a_version_7_uuid() -> None:
    assert uuid7().version == 7


def test_is_rfc_4122_variant() -> None:
    assert uuid7().variant == "specified in RFC 4122"


def test_ids_are_unique() -> None:
    assert len({uuid7() for _ in range(10_000)}) == 10_000


def test_ids_generated_later_sort_later() -> None:
    """The whole reason for v7 over v4.

    Time-ordered ids insert at the right-hand edge of a B-tree instead of scattering across it,
    which keeps index pages dense. Random primary keys are a classic way for a table to get slow
    without anything in the query changing.
    """
    first = uuid7()
    time.sleep(0.005)  # the timestamp has millisecond resolution
    second = uuid7()

    assert first < second


def test_encodes_the_current_time_in_its_high_bits() -> None:
    """Not just *ordered* — ordered by wall-clock milliseconds, which is what makes it sortable
    against ids minted by another process."""
    before = time.time_ns() // 1_000_000
    encoded_ms = uuid7().int >> 80
    after = time.time_ns() // 1_000_000

    assert before <= encoded_ms <= after
