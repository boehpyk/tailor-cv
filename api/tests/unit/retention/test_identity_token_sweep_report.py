"""Unit tests for `IdentityTokenSweepReport` (slice 2.5, T13 RED — AC-15, technical plan §0.9).

Counts only, and counts are never negative. The skeleton's `__post_init__` is a no-op (AC-6), so each
refusal is red on `DID NOT RAISE`; the zero and positive constructions are positive controls.
"""

from __future__ import annotations

import pytest

from tailorcraft.domain.retention.value_objects import IdentityTokenSweepReport
from tailorcraft.domain.shared.errors import InvariantViolated


@pytest.mark.parametrize(
    "counts",
    [(-1, 0, 0), (0, -1, 0), (0, 0, -1)],
    ids=["pending", "resets", "logins"],
)
def test_a_negative_count_is_refused(counts: tuple[int, int, int]) -> None:
    pending, resets, logins = counts
    with pytest.raises(InvariantViolated):
        IdentityTokenSweepReport(
            pending_registrations=pending, password_resets=resets, logins=logins
        )


def test_zero_counts_are_accepted() -> None:
    report = IdentityTokenSweepReport(pending_registrations=0, password_resets=0, logins=0)
    assert (report.pending_registrations, report.password_resets, report.logins) == (0, 0, 0)


def test_positive_counts_are_kept_per_kind() -> None:
    report = IdentityTokenSweepReport(pending_registrations=3, password_resets=2, logins=1)
    assert (report.pending_registrations, report.password_resets, report.logins) == (3, 2, 1)
