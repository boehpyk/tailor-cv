"""Errors the `tracking` bounded context raises when a rule about a tracked application breaks.

An error class *is* its contract, so every one here has a real body at the skeleton step (T3), as in
every other context. The domain never raises `HTTPException`; `api/errors.py` maps these to status
codes and `code`s (plan §4, the failure contract's T-rows).

**Nothing here carries text a user wrote** — never a title, a posting's title or URL, a CV label.
Ids, counts, versions and a closed status value only (Constitution §8): an exception is caught,
logged and re-raised by code that has no idea what is in its message.

This module imports `value_objects` at the top, so `ApplicationTitle.__post_init__` imports
`InvalidApplicationTitle` **inside** the method — the deferred import `BaseCvLabel` uses for the same
module-cycle reason.
"""

from __future__ import annotations

from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.tracking.value_objects import TrackedApplicationId


class TrackedApplicationNotFound(DomainError):
    """No tracked application exists with the requested id — or one exists and is not the
    requester's (raised `from TrackedApplicationNotOwnedByUser` then, so the HTTP answer is one 404
    and the use case's tests can still read the difference off `__cause__`)."""


class TrackedApplicationNotOwnedByUser(DomainError):
    """A tracked application exists, but belongs to another user.

    **Only ever a `__cause__`.** The use case raises `TrackedApplicationNotFound` from it: answering
    "not yours" instead of "not found" tells a caller that the id they guessed is real. Two types
    rather than one so a test can prove the ownership check runs at all — the shape
    `TailoringRunNotOwnedByUser` and `BaseCvNotOwnedByUser` set.
    """


class ApplicationAlreadyTracked(DomainError):
    """The run already has a card — one card per run (a unique index on `tailoring_run_id`).

    Carries the **existing card's id**, and that payload is the point: the router puts it in the 409
    body, and the client treats a repeat as success and shows the card it already has (AC-38) —
    the reason `TailoringAlreadyRunning` carries the active run's id.
    """

    def __init__(self, existing_id: TrackedApplicationId) -> None:
        super().__init__(f"this run is already tracked as {existing_id.value}")
        self.existing_id = existing_id


class TailoringRunNotTrackable(DomainError):
    """The run exists and is the user's, but has not succeeded — only a succeeded run has documents
    the user could have sent (plan §0.2).

    `status` is a **plain `str`** (the run status's wire value), not `TailoringRunStatus`:
    `domain/tracking` imports no sibling context (ADR-0029 decision 1), so the use case converts at
    the seam. A closed vocabulary, never user text, so it is safe in a message and a log line —
    `retention`'s `HistoryEntryInProgress(status: str)` is the precedent.
    """

    def __init__(self, status: str) -> None:
        super().__init__(f"a run with status {status} cannot be tracked")
        self.status = status


class TooManyTrackedApplications(DomainError):
    """The user already has the maximum number of tracked applications (500 by default).

    A use-case check, not an invariant of `TrackedApplication`: it spans every card a user has, which
    no single aggregate can know. **Soft**, like `TooManyTailoringRuns`: two simultaneous tracks may
    overshoot by one, and that is accepted rather than locked. It is also what bounds the board, which
    is rendered whole (ADR-0024 amendment (a)).
    """

    def __init__(self, cap: int) -> None:
        super().__init__(f"the limit of {cap} tracked applications is reached")
        self.cap = cap


class TrackedApplicationVersionConflict(DomainError):
    """A move or retitle was made against a `version` the card has already moved past (T-20).

    Raised by the **aggregate**, from its own compare — and **before** the no-op rule (OQ-11), so a
    stale tab is told it is stale even when the stage it wants already holds. Carries both numbers;
    the router puts `current_version` in the 409 body. The database-level race that slips past this
    compare is `TrackedApplicationConcurrentlyModified`, found by a different mechanism in a
    different place — so two types, as `TailoredDocumentVersionConflict` and
    `TailoringRunConcurrentlyModified` are (ADR-0015 §3). No shared base with either: shared shape,
    not shared rule.
    """

    def __init__(self, expected_version: int, current_version: int) -> None:
        super().__init__(
            f"expected tracked application version {expected_version}, "
            f"but it is at {current_version}"
        )
        self.expected_version = expected_version
        self.current_version = current_version


class TrackedApplicationConcurrentlyModified(DomainError):
    """The card's row changed or vanished between loading the aggregate and saving it (T-21).

    **Raised by the repository, never by the aggregate**, which cannot see another process: the
    mapping declares `version` as `version_id_col`, SQLAlchemy's `StaleDataError` on zero rows is
    translated into this, and the router answers 409 with `current_version: null`.
    """


class InvalidApplicationTitle(DomainError):
    """A title was empty after trimming, longer than 120 characters, or held a control character
    (AC-2). The message names the rule broken and **never quotes the value**."""
