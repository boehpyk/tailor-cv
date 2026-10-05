"""Use cases for the `tracking` bounded context (slice 3.1): put a succeeded run on the board, move
it between stages, retitle it, take it off, and show the whole board.

One module per user intent. Every use case resolves the user first (`UserNotFound` for an erased
account behind a still-valid token) and authorizes a card by **one equality**,
``card.user_id == user_id``, collapsing "not mine" into `TrackedApplicationNotFound` with
`TrackedApplicationNotOwnedByUser` on `__cause__` (the 404 collapse, as `GetTailoringRun`).

**This is the seam between `tailoring` and `tracking`.** `domain/tracking` imports no sibling
context (ADR-0029 decision 1), so `TrackApplication` authorizes the run through tailoring's own
`GetTailoringRun` and converts at exactly one place: ``TrackedRunRef(run.id.value)``.

**No logging here**, and no queue: nothing in this context is retried, so there is no redelivery to
design for (plan §2). The transaction boundary is the router's; events are published by the use
case after the repository call returns.
"""
