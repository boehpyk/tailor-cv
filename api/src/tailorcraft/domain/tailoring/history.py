"""The tailoring history read model: what a signed-in user's history page is made of (slice 2.3).

**A read model, not an aggregate** (ADR-0024). Nothing here has behaviour, records an event or is
ever saved: `TailoringHistoryQuery.page_for_user` builds these straight from one SQL query that
LEFT JOINs a run to its base CV and its posting, and hands them to a router. The shape is decided
by what the page shows, not by what the aggregates hold — which is why a `TailoringHistoryEntry` is
not a `TailoringRun`, and why neither document body appears anywhere in this module: a history row
lists runs, and loading two documents per row to list them is the cost 2.2's T30 measured.

**Plain strings for `title`, `label` and `original_filename`, not their value objects.** A read
model is not re-validated on the way out — every one of those strings was validated on the way in,
by the aggregate that stored it — and constructing a validating value object per row per page is
the per-load cost lesson of 2.2 (`ExtractedText.__post_init__` on the event loop). Ids and enums
keep their types, because they cost nothing and a transposed UUID is still a bug worth a type.

Two types here validate: `HistoryCursor` and `HistoryPageSize` are **input** — a cursor and a page
size arrive from a query string a stranger wrote — so they are refused in `__post_init__` like any
value object with rules. The other four are output, pure data, and complete at the skeleton step.

`retryable` is deliberately not here: whether "Try again" is worth offering is the API's rule
(`_is_retryable` in the tailoring router, Constitution §4.5), exactly as in 1.3's run response.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId, PostingSource
from tailorcraft.domain.tailoring.value_objects import (
    TailoringFailureReason,
    TailoringRunId,
    TailoringRunStatus,
)


@dataclass(frozen=True, slots=True)
class HistoryCursor:
    """Where the next page starts: the last entry's `(requested_at, tailoring_run_id)` (ADR-0024).

    Keyset, not offset: a history that grows while someone pages through it must neither repeat nor
    skip a row, and `(requested_at, id)` is a total order because the id breaks every tie. Unsigned
    on purpose (ADR-0024) — the query is always scoped to the requester, so a forged cursor can only
    move a user around their own history.

    `requested_at` must be timezone-aware and whole-second (the `Clock` contract, ADR-0007), since a
    cursor that could never have come from a stored value would silently match nothing →
    `InvalidHistoryCursor`.
    """

    requested_at: datetime
    tailoring_run_id: TailoringRunId

    def __post_init__(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class HistoryPageSize:
    """How many entries one page holds: `1..MAXIMUM`, `DEFAULT` when the caller names none.

    Out of range → `InvalidHistoryPageSize`. The ceiling is a cost bound, not a preference: the size
    is caller-chosen, and an unbounded one is "every row at once" asked for by a query string.
    """

    DEFAULT: ClassVar[int] = 20
    MAXIMUM: ClassVar[int] = 50

    value: int

    def __post_init__(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class HistoryPosting:
    """The posting half of an entry. The entry holds `None` instead when the posting row is gone."""

    job_posting_id: JobPostingId
    source: PostingSource
    title: str | None
    source_url: str | None
    # At most 140 characters of the posting's text, cut in SQL so the whole text is never selected.
    preview: str


@dataclass(frozen=True, slots=True)
class HistoryBaseCv:
    """The base CV half of an entry, present only while the saved CV still exists (OQ-2: deleting a
    saved CV leaves its history, and the entry then says "CV deleted")."""

    base_cv_id: BaseCvId
    label: str | None
    original_filename: str


@dataclass(frozen=True, slots=True)
class TailoringHistoryEntry:
    """One run in a user's history, with what is left of its two inputs.

    `base_cv_id` is always present — it is the run's own reference, and the run keeps it after the
    CV is deleted (ADR-0014 amendment (b): a dangling reference plus derived state). `base_cv` is
    `None` exactly when that CV no longer exists; the two fields are different facts on purpose.
    """

    tailoring_run_id: TailoringRunId
    status: TailoringRunStatus
    failure_reason: TailoringFailureReason | None
    requested_at: datetime
    completed_at: datetime | None
    version: int
    edited: bool
    base_cv_id: BaseCvId
    base_cv: HistoryBaseCv | None
    posting: HistoryPosting | None


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One page of entries, newest first, and where the next one starts — `None` on the last page."""

    entries: tuple[TailoringHistoryEntry, ...]
    next_cursor: HistoryCursor | None
