"""AC-1: the claim's two value objects (`domain/identity/claim.py`), written from the spec.

`ClaimedGuestWork` is what `GuestWorkClaimPort.transfer` hands back; `GuestWorkClaimReport` is what
the use case returns after trying the unlinks. Pure: no I/O, no fixtures, no event loop, no mocks.

Refusals are asserted with `pytest.raises(InvariantViolated)` — the exact type. Never `RuntimeError`:
`NotImplementedError` subclasses it, so such a test would pass vacuously against the skeleton.
"""

from __future__ import annotations

import dataclasses
from typing import Any
from uuid import UUID

import pytest

from tailorcraft.domain.identity.claim import ClaimedGuestWork, GuestWorkClaimReport
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.domain.shared.files import FileRef

_COUNT_FIELDS = (
    "base_cvs",
    "job_postings",
    "tailoring_runs",
    "export_jobs",
    "working_copies_dropped",
)
_ROW_COUNT_FIELDS = ("base_cvs", "job_postings", "tailoring_runs", "export_jobs")


def _ref(n: int) -> FileRef:
    """A distinct, well-formed key per `n` (the grammar is `FileRef`'s own business)."""
    return FileRef.for_base_cv(
        BaseCvId(UUID(f"00000000-0000-7000-8000-{n:012d}")), CvContentType.PDF
    )


def _claimed(**overrides: Any) -> ClaimedGuestWork:
    fields: dict[str, Any] = {
        "base_cvs": 1,
        "job_postings": 2,
        "tailoring_runs": 3,
        "export_jobs": 4,
        "working_copies_dropped": 2,
        "files_to_unlink": (_ref(1), _ref(2)),
    }
    fields.update(overrides)
    return ClaimedGuestWork(**fields)


def _report(**overrides: Any) -> GuestWorkClaimReport:
    fields: dict[str, Any] = {
        "base_cvs": 1,
        "job_postings": 2,
        "tailoring_runs": 3,
        "export_jobs": 4,
        "working_copies_dropped": 2,
        "files_unlinked": 2,
        "unlink_failures": (),
    }
    fields.update(overrides)
    return GuestWorkClaimReport(**fields)


# ====================================================================================================
# ClaimedGuestWork
# ====================================================================================================


def test_claimed_guest_work_holds_what_it_was_given() -> None:
    claimed = _claimed()

    assert (
        claimed.base_cvs,
        claimed.job_postings,
        claimed.tailoring_runs,
        claimed.export_jobs,
        claimed.working_copies_dropped,
        claimed.files_to_unlink,
    ) == (1, 2, 3, 4, 2, (_ref(1), _ref(2)))


def test_a_claim_that_moved_and_dropped_nothing_is_valid() -> None:
    claimed = _claimed(
        base_cvs=0,
        job_postings=0,
        tailoring_runs=0,
        export_jobs=0,
        working_copies_dropped=0,
        files_to_unlink=(),
    )

    assert claimed.files_to_unlink == ()


@pytest.mark.parametrize("field", _COUNT_FIELDS)
def test_claimed_guest_work_refuses_a_negative_count(field: str) -> None:
    with pytest.raises(InvariantViolated):
        _claimed(**{field: -1})


@pytest.mark.parametrize("field", _COUNT_FIELDS)
def test_claimed_guest_work_accepts_a_zero_count(field: str) -> None:
    # A zero must not be mistaken for a negative: the boundary is `< 0`, not `<= 0`.
    overrides: dict[str, Any] = {field: 0}
    if field == "working_copies_dropped":
        overrides["files_to_unlink"] = ()

    assert getattr(_claimed(**overrides), field) == 0


def test_claimed_guest_work_refuses_more_files_to_unlink_than_working_copies_dropped() -> None:
    """A key with no dropped row behind it is a file this claim had no authority to unlink."""
    with pytest.raises(InvariantViolated):
        _claimed(working_copies_dropped=1, files_to_unlink=(_ref(1), _ref(2)))


def test_claimed_guest_work_refuses_a_file_when_nothing_was_dropped() -> None:
    with pytest.raises(InvariantViolated):
        _claimed(working_copies_dropped=0, files_to_unlink=(_ref(1),))


def test_claimed_guest_work_allows_fewer_files_than_working_copies_dropped() -> None:
    """The inequality is `<=`: the docstring allows a shorter tuple, never a longer one."""
    claimed = _claimed(working_copies_dropped=3, files_to_unlink=(_ref(1),))

    assert claimed.files_to_unlink == (_ref(1),)


def test_claimed_guest_work_with_equal_fields_are_equal() -> None:
    assert _claimed() == _claimed()


def test_claimed_guest_work_differing_in_a_file_key_are_not_equal() -> None:
    assert _claimed() != _claimed(files_to_unlink=(_ref(1), _ref(3)))


def test_claimed_guest_work_is_frozen_and_slotted() -> None:
    claimed = _claimed()

    assert dataclasses.is_dataclass(ClaimedGuestWork)
    assert ClaimedGuestWork.__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert hasattr(ClaimedGuestWork, "__slots__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        claimed.base_cvs = 9  # type: ignore[misc]


# ====================================================================================================
# GuestWorkClaimReport
# ====================================================================================================


@pytest.mark.parametrize("field", [*_COUNT_FIELDS, "files_unlinked"])
def test_report_refuses_a_negative_count(field: str) -> None:
    with pytest.raises(InvariantViolated):
        _report(**{field: -1})


def test_report_with_equal_fields_are_equal() -> None:
    assert _report(unlink_failures=("OSError",)) == _report(unlink_failures=("OSError",))


def test_report_differing_in_a_failure_name_are_not_equal() -> None:
    assert _report(unlink_failures=("OSError",)) != _report(unlink_failures=("PermissionError",))


def test_report_is_frozen_and_slotted() -> None:
    report = _report()

    assert GuestWorkClaimReport.__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert hasattr(GuestWorkClaimReport, "__slots__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.files_unlinked = 9  # type: ignore[misc]


def test_nothing_is_all_zeros() -> None:
    report = GuestWorkClaimReport.nothing()

    assert (
        report.base_cvs,
        report.job_postings,
        report.tailoring_runs,
        report.export_jobs,
        report.working_copies_dropped,
        report.files_unlinked,
        report.unlink_failures,
    ) == (0, 0, 0, 0, 0, 0, ())


def test_nothing_equals_a_report_constructed_with_every_count_zero() -> None:
    assert GuestWorkClaimReport.nothing() == _report(
        base_cvs=0,
        job_postings=0,
        tailoring_runs=0,
        export_jobs=0,
        working_copies_dropped=0,
        files_unlinked=0,
        unlink_failures=(),
    )


def test_nothing_did_not_claim_anything() -> None:
    assert GuestWorkClaimReport.nothing().claimed_anything is False


@pytest.mark.parametrize("field", _ROW_COUNT_FIELDS)
def test_claimed_anything_is_true_when_only_that_row_count_is_positive(field: str) -> None:
    report = GuestWorkClaimReport.nothing()
    overrides: dict[str, Any] = {field: 1}
    only_this = dataclasses.replace(report, **overrides)

    assert only_this.claimed_anything is True


def test_dropped_working_copies_alone_do_not_count_as_claiming_anything() -> None:
    """Dropped copies are discarded, not claimed (ADR-0022 amendment (d)); the banner must not
    announce work that moved when none did."""
    report = dataclasses.replace(
        GuestWorkClaimReport.nothing(), working_copies_dropped=3, files_unlinked=3
    )

    assert report.claimed_anything is False


def test_files_unlinked_alone_do_not_count_as_claiming_anything() -> None:
    report = dataclasses.replace(GuestWorkClaimReport.nothing(), files_unlinked=2)

    assert report.claimed_anything is False


# ---- of() -----------------------------------------------------------------------------------------


def test_of_maps_the_four_row_counts_and_the_dropped_count() -> None:
    report = GuestWorkClaimReport.of(_claimed(), ())

    assert (
        report.base_cvs,
        report.job_postings,
        report.tailoring_runs,
        report.export_jobs,
        report.working_copies_dropped,
    ) == (1, 2, 3, 4, 2)


def test_of_counts_every_unlink_as_done_when_none_failed() -> None:
    report = GuestWorkClaimReport.of(_claimed(), ())

    assert (report.files_unlinked, report.unlink_failures) == (2, ())


def test_of_subtracts_failures_from_files_unlinked_and_keeps_their_names() -> None:
    report = GuestWorkClaimReport.of(_claimed(), ["PermissionError"])

    assert (report.files_unlinked, report.unlink_failures) == (1, ("PermissionError",))


def test_of_with_every_unlink_failing_reports_zero_unlinked() -> None:
    report = GuestWorkClaimReport.of(_claimed(), ["OSError", "PermissionError"])

    assert (report.files_unlinked, report.unlink_failures) == (0, ("OSError", "PermissionError"))


def test_of_unlinked_plus_failures_equals_the_files_to_unlink() -> None:
    """AC-1's accounting identity, over every split of three files."""
    claimed = _claimed(working_copies_dropped=3, files_to_unlink=(_ref(1), _ref(2), _ref(3)))

    for failed in range(4):
        report = GuestWorkClaimReport.of(claimed, ["OSError"] * failed)

        assert report.files_unlinked + len(report.unlink_failures) == len(claimed.files_to_unlink)


def test_of_with_no_files_to_unlink_reports_zero_unlinked() -> None:
    claimed = _claimed(working_copies_dropped=0, files_to_unlink=())

    assert GuestWorkClaimReport.of(claimed, ()).files_unlinked == 0


def test_of_stores_failures_as_a_tuple_whatever_sequence_it_was_given() -> None:
    report = GuestWorkClaimReport.of(_claimed(), ["OSError"])

    assert isinstance(report.unlink_failures, tuple)


def test_of_a_claim_that_moved_work_claimed_something() -> None:
    assert GuestWorkClaimReport.of(_claimed(), ()).claimed_anything is True


def test_of_a_claim_that_moved_no_rows_but_dropped_a_copy_claimed_nothing() -> None:
    claimed = _claimed(
        base_cvs=0,
        job_postings=0,
        tailoring_runs=0,
        export_jobs=0,
        working_copies_dropped=1,
        files_to_unlink=(_ref(1),),
    )

    assert GuestWorkClaimReport.of(claimed, ()).claimed_anything is False
