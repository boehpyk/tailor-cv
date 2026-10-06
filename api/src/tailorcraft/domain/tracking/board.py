"""The application board read model: what a signed-in user's board is made of (ADR-0024 and its
amendment (a)).

**A read model, not an aggregate.** Nothing here has behaviour, records an event or is ever saved:
`ApplicationBoardQuery.board_for_user` builds these straight from one SQL statement that LEFT JOINs a
card to its run, the run's posting and the run's base CV — owner in every join condition — and hands
them to a router. The shape is decided by what a card shows, which is why a `BoardCard` is not a
`TrackedApplication`, and why no document body appears anywhere in this module.

**Plain strings for titles, labels, filenames and the posting preview**, not their value objects
(ADR-0024 decision 2): every one was validated on the way in, and re-validating per card per load is
2.2's `ExtractedText.__post_init__` lesson.

**Ids from other contexts are bare `UUID`s here, and `source` is a plain `str`** — a deliberate
departure from `tailoring/history.py`, which types them `JobPostingId`, `BaseCvId` and
`PostingSource`. `domain/tracking` imports no sibling context (ADR-0029 decision 1, the owner's T0
amendment), so those types are not available, and minting tracking-owned copies of them for a
display-only projection would be types without a rule. The run is the exception — `TrackedRunRef` —
because it is the reference this context actually holds.

`run`, `posting` and `base_cv` are each `None` when their LEFT JOIN found nothing: a deleted saved CV
(derived at read time, as in history), or — should it ever happen — a vanished run, which stays
visible so it stays removable (plan §0.8, T-36).

Complete at the skeleton step (T3): pure data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from tailorcraft.domain.tracking.value_objects import (
    ApplicationStage,
    TrackedApplicationId,
    TrackedRunRef,
)


@dataclass(frozen=True, slots=True)
class BoardPosting:
    """The posting a card's run was tailored to: enough to recognise it, never its full text.

    `preview` is at most 140 characters, cut in SQL (`left(text, 140)`); `source` is the posting
    source's wire value (`"pasted"` or `"fetched"`).
    """

    job_posting_id: UUID
    source: str
    title: str | None
    source_url: str | None
    preview: str


@dataclass(frozen=True, slots=True)
class BoardBaseCv:
    """The base CV a card's run started from — label and filename only."""

    base_cv_id: UUID
    label: str | None
    original_filename: str


@dataclass(frozen=True, slots=True)
class BoardRun:
    """The run a card refers to: when it was requested, and whether the user edited its documents."""

    tailoring_run_id: TrackedRunRef
    requested_at: datetime
    edited: bool


@dataclass(frozen=True, slots=True)
class BoardCard:
    """One card on the board: the card's own fields, plus what its three LEFT JOINs found."""

    id: TrackedApplicationId
    tailoring_run_id: TrackedRunRef
    stage: ApplicationStage
    title: str | None
    tracked_at: datetime
    stage_changed_at: datetime
    version: int
    run: BoardRun | None
    posting: BoardPosting | None
    base_cv: BoardBaseCv | None


@dataclass(frozen=True, slots=True)
class ApplicationBoard:
    """Every card a user has, newest stage change first — **unpaginated, deliberately** (ADR-0024
    amendment (a)): a board must show every column at once, and it is bounded at write time by
    `MAX_TRACKED_APPLICATIONS_PER_USER`. Grouping into columns is the client's (`ApplicationStage`'s
    declaration order)."""

    cards: tuple[BoardCard, ...]
