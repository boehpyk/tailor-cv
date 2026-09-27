"""`domain/tailoring/history.py` — the tailoring history read model (2.3's AC-6).

Pure domain tests: no I/O, no fixtures, no event loop, no mocks. `HistoryCursor` and
`HistoryPageSize` are the two **input** types — a cursor and a page size arrive from a query string
a stranger wrote — so they validate in `__post_init__` like any other value object with rules. The
other four types are **output**: pure data, complete at the skeleton step, behaviour-free.

**The skeleton's `__post_init__` on the two input types raises `NotImplementedError`
unconditionally — for valid input as well as invalid.** Every test below that constructs a
`HistoryCursor` or a `HistoryPageSize` therefore fails red on that `NotImplementedError` right now,
never on `InvalidHistoryCursor` / `InvalidHistoryPageSize` and never on the assertion that would
follow a successful construction. That is the expected shape of this file's red — recorded in the
RED commit body — and it is exactly what T7's GREEN replaces with real validation. The four output
types below have no such gap: they are complete already, so their tests pass on arrival, the same
way J-2/J-4 do in `test_job_posting.py`.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import UUID

import pytest

from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId, PostingSource
from tailorcraft.domain.tailoring.errors import InvalidHistoryCursor, InvalidHistoryPageSize
from tailorcraft.domain.tailoring.history import (
    HistoryBaseCv,
    HistoryCursor,
    HistoryPage,
    HistoryPageSize,
    HistoryPosting,
    TailoringHistoryEntry,
)
from tailorcraft.domain.tailoring.value_objects import TailoringRunId, TailoringRunStatus

_RUN_ID = TailoringRunId(value=UUID("0192f0a1-89ab-7cde-8123-456789abcde0"))
_OTHER_RUN_ID = TailoringRunId(value=UUID("44444444-4444-7444-8444-444444444444"))
_BASE_CV_ID = BaseCvId(value=UUID("22222222-2222-7222-8222-222222222222"))
_JOB_POSTING_ID = JobPostingId(value=UUID("33333333-3333-7333-8333-333333333333"))
# Whole-second and tz-aware, per ADR-0007 — the one shape a real cursor is ever handed back as.
_VALID_REQUESTED_AT = datetime(2026, 9, 10, 10, 0, 0, tzinfo=UTC)


# --- HistoryCursor: input, validated ----------------------------------------------------------------


def test_history_cursor_accepts_a_tz_aware_whole_second_requested_at() -> None:
    """The one shape a real cursor round-trips as (ADR-0024): whatever the query returned for the
    last row on the previous page."""
    cursor = HistoryCursor(requested_at=_VALID_REQUESTED_AT, tailoring_run_id=_RUN_ID)

    assert cursor.requested_at == _VALID_REQUESTED_AT
    assert cursor.tailoring_run_id == _RUN_ID


def test_history_cursor_rejects_a_naive_requested_at() -> None:
    """A cursor is an echo of a value the server handed out — a naive datetime could never have
    come from a whole-second, timezone-aware `Clock` (ADR-0007), so it is refused rather than
    silently matching nothing."""
    naive = datetime(2026, 9, 10, 10, 0, 0)

    with pytest.raises(InvalidHistoryCursor):
        HistoryCursor(requested_at=naive, tailoring_run_id=_RUN_ID)


def test_history_cursor_rejects_a_sub_second_requested_at() -> None:
    """The `Clock` port is whole-second by contract (ADR-0007); a microsecond-bearing value could
    never have come from a stored `requested_at` either."""
    sub_second = _VALID_REQUESTED_AT.replace(microsecond=1)

    with pytest.raises(InvalidHistoryCursor):
        HistoryCursor(requested_at=sub_second, tailoring_run_id=_RUN_ID)


def test_history_cursor_equality_is_by_value() -> None:
    a = HistoryCursor(requested_at=_VALID_REQUESTED_AT, tailoring_run_id=_RUN_ID)
    b = HistoryCursor(requested_at=_VALID_REQUESTED_AT, tailoring_run_id=_RUN_ID)

    assert a == b


def test_history_cursor_inequality_on_a_different_run_id() -> None:
    """The id breaks every tie (ADR-0024): two rows in the same whole second must compare unequal
    on the cursor, or a page boundary could skip or repeat one of them."""
    a = HistoryCursor(requested_at=_VALID_REQUESTED_AT, tailoring_run_id=_RUN_ID)
    b = HistoryCursor(requested_at=_VALID_REQUESTED_AT, tailoring_run_id=_OTHER_RUN_ID)

    assert a != b


def test_history_cursor_is_frozen() -> None:
    cursor = HistoryCursor(requested_at=_VALID_REQUESTED_AT, tailoring_run_id=_RUN_ID)

    with pytest.raises(dataclasses.FrozenInstanceError):
        cursor.tailoring_run_id = _OTHER_RUN_ID  # type: ignore[misc]


# --- HistoryPageSize: input, validated --------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 50], ids=["floor", "ceiling"])
def test_history_page_size_accepts_the_boundary_values(value: int) -> None:
    """1..50 inclusive (AC-6) — the floor (a page of one is still a page) and the ceiling (the cost
    bound a query string could otherwise ask past)."""
    size = HistoryPageSize(value)

    assert size.value == value


@pytest.mark.parametrize("value", [0, 51], ids=["below_floor", "above_ceiling"])
def test_history_page_size_rejects_values_outside_the_range(value: int) -> None:
    with pytest.raises(InvalidHistoryPageSize):
        HistoryPageSize(value)


def test_history_page_size_default_is_twenty() -> None:
    """`DEFAULT` is a `ClassVar`, not validated by `__post_init__` — reading it never touches the
    skeleton's `NotImplementedError`, so this passes on arrival."""
    assert HistoryPageSize.DEFAULT == 20


def test_history_page_size_maximum_is_fifty() -> None:
    assert HistoryPageSize.MAXIMUM == 50


def test_history_page_size_equality_is_by_value() -> None:
    assert HistoryPageSize(20) == HistoryPageSize(20)


def test_history_page_size_inequality_on_a_different_value() -> None:
    assert HistoryPageSize(1) != HistoryPageSize(50)


def test_history_page_size_is_frozen() -> None:
    size = HistoryPageSize(20)

    with pytest.raises(dataclasses.FrozenInstanceError):
        size.value = 1  # type: ignore[misc]


# --- HistoryPosting, HistoryBaseCv, TailoringHistoryEntry, HistoryPage: output, pure data -----------
#
# Complete at the skeleton step (T5b) — no `__post_init__`, nothing to defer to GREEN — so every
# test below passes immediately, exactly as J-2/J-4 do in `test_job_posting.py`. They pin the shape
# against a future edit that adds behaviour or a validating `__post_init__` where the spec says
# there should be none.


def _a_history_posting() -> HistoryPosting:
    return HistoryPosting(
        job_posting_id=_JOB_POSTING_ID,
        source=PostingSource.PASTED,
        title=None,
        source_url=None,
        preview="a" * 140,
    )


def _a_history_base_cv() -> HistoryBaseCv:
    return HistoryBaseCv(base_cv_id=_BASE_CV_ID, label="My CV", original_filename="cv.pdf")


def _a_history_entry(
    *, base_cv: HistoryBaseCv | None = None, posting: HistoryPosting | None = None
) -> TailoringHistoryEntry:
    return TailoringHistoryEntry(
        tailoring_run_id=_RUN_ID,
        status=TailoringRunStatus.SUCCEEDED,
        failure_reason=None,
        requested_at=_VALID_REQUESTED_AT,
        completed_at=_VALID_REQUESTED_AT,
        version=1,
        edited=False,
        base_cv_id=_BASE_CV_ID,
        base_cv=base_cv,
        posting=posting,
    )


def test_history_posting_round_trips_its_fields() -> None:
    posting = _a_history_posting()

    assert posting.job_posting_id == _JOB_POSTING_ID
    assert posting.source is PostingSource.PASTED
    assert posting.title is None
    assert posting.source_url is None
    assert posting.preview == "a" * 140


def test_history_base_cv_round_trips_its_fields() -> None:
    base_cv = _a_history_base_cv()

    assert base_cv.base_cv_id == _BASE_CV_ID
    assert base_cv.label == "My CV"
    assert base_cv.original_filename == "cv.pdf"


def test_tailoring_history_entry_holds_base_cv_id_even_when_the_saved_cv_is_gone() -> None:
    """OQ-2: `base_cv_id` is the run's own reference and survives the CV's deletion (ADR-0014
    amendment (b), a dangling reference plus derived state); `base_cv` is `None` exactly when the
    saved CV no longer exists — two different facts on purpose, never collapsed into one optional."""
    entry = _a_history_entry(base_cv=None)

    assert entry.base_cv_id == _BASE_CV_ID
    assert entry.base_cv is None


def test_tailoring_history_entry_carries_the_base_cv_when_it_still_exists() -> None:
    base_cv = _a_history_base_cv()
    entry = _a_history_entry(base_cv=base_cv)

    assert entry.base_cv == base_cv


def test_tailoring_history_entry_posting_is_none_when_the_posting_row_is_gone() -> None:
    """H-30: a posting can go missing (never deleted by this slice's own code, but the read model
    does not assume it is always there) — `posting` is `None` rather than a row with blank fields."""
    entry = _a_history_entry(posting=None)

    assert entry.posting is None


def test_history_page_round_trips_entries_and_a_none_next_cursor() -> None:
    entry = _a_history_entry()

    page = HistoryPage(entries=(entry,), next_cursor=None)

    assert page.entries == (entry,)
    assert page.next_cursor is None


@pytest.mark.parametrize(
    "make_instance",
    [_a_history_posting, _a_history_base_cv, _a_history_entry, lambda: HistoryPage((), None)],
    ids=["HistoryPosting", "HistoryBaseCv", "TailoringHistoryEntry", "HistoryPage"],
)
def test_output_types_are_frozen_and_slotted(make_instance: object) -> None:
    """AC-6: 'frozen, slotted, behaviour-free' checked structurally rather than by trusting the
    decorator's presence — `@dataclass(frozen=True, slots=True)` produces exactly these two
    properties, and a future edit that dropped either argument would leave this test to notice."""
    instance = make_instance()  # type: ignore[operator]

    assert not hasattr(instance, "__dict__")
    first_field = dataclasses.fields(instance)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, first_field, getattr(instance, first_field))


@pytest.mark.parametrize(
    "value_type",
    [HistoryPosting, HistoryBaseCv, TailoringHistoryEntry, HistoryPage],
    ids=["HistoryPosting", "HistoryBaseCv", "TailoringHistoryEntry", "HistoryPage"],
)
def test_output_types_have_no_public_methods(value_type: type) -> None:
    """Behaviour-free (AC-6): a read model is data the router assembles into a response, never an
    object anyone calls a method on — the same argument `domain/identity/ownership.py` makes for why
    `Owner`'s two variants carry none."""
    public_methods = {
        name
        for name, value in vars(value_type).items()
        if not name.startswith("__") and callable(value)
    }

    assert public_methods == set()
