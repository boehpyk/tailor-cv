"""Domain events for the `posting` bounded context.

Same rule as `domain/intake/events.py`, and it matters at least as much here: an event carries **ids
and value objects only** — never the aggregate, never the job description, never the URL, never the
title. An event payload reaches every listener and every log line at once
(`domain/shared/events.py`), and `LoggingEventPublisher`
(`infrastructure/events/logging_publisher.py`) logs *every field of every event it receives*. So an
event's field set is, quite literally, a log field set: adding a field here is the same act as
adding a column to a log line, and it should be made with that in mind rather than as a convenience
for one subscriber.

**Why a URL is PII in this product.** It is tempting to treat `https://boards.example.com/jobs/4821`
as harmless — it is a public page anyone can visit. It is not the *page* that is sensitive; it is
the *pairing*. A log line saying "session X captured this posting" records which job a named person
is applying for, alongside a CV that carries their name and address. That is Constitution §8's
concern exactly, and it is the direct analogue of 1.1's decision to keep the original filename out
of `BaseCvUploaded`.

These are pure data with no behaviour: a dataclass field list *is* the whole signature, so unlike
the aggregate there is nothing to defer to a GREEN step — this module is written whole at the
skeleton stage. AC-19 asserts the field set below rather than trusting this docstring, which is the
only version of the rule that can actually fail.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.posting.value_objects import JobPostingId, PostingSource
from tailorcraft.domain.shared.events import DomainEvent


@dataclass(frozen=True, slots=True, kw_only=True)
class JobPostingCaptured(DomainEvent):
    """A job posting now exists for this session — however it got here.

    Payload: `job_posting_id`, `guest_session_id`, `source`, `character_count` (+ inherited
    `occurred_at`). **Deliberately absent: the text, the URL and the title.** `character_count`
    exists on `JobPostingText` for precisely this reason — so that a subscriber reporting on how
    much text there is never needs to hold the text.

    **One event, not two.** `JobPostingPasted` and `JobPostingFetched` would be the obvious pair,
    and they would be a mistake: every future subscriber — 1.3's tailoring trigger first — would
    have to handle two names for one fact, and each new subscriber would be one more place to
    forget the second name. There is a single fact here, *a posting now exists for this session*,
    and `source` is a **field** of that fact rather than a second fact. A subscriber that cares
    branches on the field; one that does not ignores it, which is the case that matters because it
    is the common one.

    **There is no event for a failed fetch**, and its absence is a decision (ADR-0013). Events are
    recorded by aggregates, and a failed fetch produces no aggregate — no bytes, no text, no row.
    Inventing a `JobPostingFetchFailed` event would mean recording it on nothing, or manufacturing
    an aggregate for the sole purpose of having somewhere to hang it. The failure is a log line at
    the adapter and an HTTP error at the boundary; that is the whole of it.
    """

    job_posting_id: JobPostingId
    guest_session_id: GuestSessionId
    source: PostingSource
    character_count: int
