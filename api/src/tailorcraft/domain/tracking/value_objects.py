"""Value objects for the `tracking` context: a card's identity, the run it refers to, its stage and
its title.

The house rule, as in every other context: a field with a rule gets its own type, frozen and slotted,
validating in `__post_init__`, compared by value — never Pydantic (ADR-0002).

Three of the four types here have nothing to enforce and are complete as written: two typed UUIDs
and a closed enum. `ApplicationTitle` is the one with rules, and at the skeleton step (T3) its
`__post_init__` is a deliberate **no-op**, not a `NotImplementedError`: AC-2's "refused" tests must
go red on `DID NOT RAISE`, which only discriminates if constructing a title succeeds (2.4's lesson —
a raising stub turns every refusal test green for the wrong reason, because `NotImplementedError` is
a `RuntimeError` and is caught by nothing the test meant).

**Ids are frozen dataclasses with one `value: UUID`, not `typing.NewType`.** ADR-0029 decision 1
and the technical plan §1 describe `TrackedRunRef` as "a `NewType` over `UUID`", and the plan
describes `TrackedApplicationId` as a `NewType` "as `TailoringRunId`". But `TailoringRunId` — and
every other id in this codebase (`UserId`, `BaseCvId`, `JobPostingId`, …) — is a frozen dataclass,
and the plan's phrase "as `TailoringRunId`" is the stronger instruction. The dataclass keeps the
distinction alive at *runtime* too (a `NewType` is erased, so `TrackedRunRef(x) == x` holds and a
bare `UUID` flowing into an event compares equal to the right one by accident). The call shape is
identical either way: `TrackedRunRef(some_uuid)`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TrackedApplicationId:
    """A `TrackedApplication`'s identity, typed so it cannot be handed where a run's id was meant.

    A card holds two UUIDs of different things — its own and its run's — and they are 1:1, which
    makes transposing them the quiet kind of bug: `get(card.tailoring_run_id)` would find nothing,
    or worse, look plausible in a test that happened to reuse one UUID for both.
    """

    value: UUID

    # No `__post_init__`: every `UUID` is a valid `TrackedApplicationId` (`TailoringRunId`'s reason).


@dataclass(frozen=True, slots=True)
class TrackedRunRef:
    """The tailoring run a card refers to — held as **tracking's own type**, not `TailoringRunId`.

    `domain/tracking` imports no sibling context (ADR-0029 decision 1, the owner's T0 amendment), so
    the reference cannot be `tailoring`'s type. It is still a reference rather than a bare `UUID`, for
    `TrackedApplicationId`'s reason. `application/tracking/` converts at the seam
    (`TrackedRunRef(run.id.value)`), and that conversion is the only place the two contexts' types
    meet. There is no foreign key behind it either (ADR-0029, plan §0.7): contexts' tables are not
    fused, and the race an FK would close is closed by two locks in the repository.
    """

    value: UUID

    # No `__post_init__`: whether the run exists, is the user's and has succeeded is the use case's
    # question, asked through `tailoring`'s own port — not something a UUID can know.


class ApplicationStage(StrEnum):
    """Where the user believes an application stands. Six values, closed, **in board order** (AC-1,
    ADR-0029 decision 3).

    Declaration order is the column order on the board, and iterating the enum is how anything reads
    it — so reordering these members reorders the board. Stored as `VARCHAR(16)` with a CHECK, not
    a native Postgres enum (adding a member to one takes a lock; `tailoring_run.status`'s reason).

    Any stage may move to any other (TA-2): see `TrackedApplication`'s docstring for why this is not
    `TailoringRun`'s strict table.
    """

    TO_APPLY = "to_apply"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class ApplicationTitle:
    """The short label a user gives a card, because a pasted posting has no title and *"About us: We
    are a fast-growing…"* is not something anyone can scan (OQ-5).

    Trimmed; 1 to `MAX_LENGTH` (120) characters after trimming; no Unicode control character
    (category `Cc` — tab, CR, LF and NUL included) — refused with `InvalidApplicationTitle`, whose
    message never quotes the value (AC-2). A display string, never markup: React renders it as text.
    It is user text, so **no domain event ever carries one** (AC-6).
    """

    MAX_LENGTH: ClassVar[int] = 120

    value: str

    def __post_init__(self) -> None:
        """Trim, then refuse empty, over-length and control characters (T5).

        A **no-op** at the skeleton step, on purpose — see the module docstring.
        """
