"""Domain events for the `intake` bounded context.

Every event here carries **ids and value objects only** — never the aggregate, never the uploaded
bytes, never a fragment of extracted text, and never the original filename. An event payload reaches
every listener and every log line at once (`domain/shared/events.py`), and this product's densest
PII arrives in this exact context (Constitution §8): a CV body or a person's filename leaking into a
log line is not a hypothetical here, it is the first thing that would happen if a future event added
one of those fields without thinking about where the event goes.

`LoggingEventPublisher` (`infrastructure/events/logging_publisher.py`) logs every field of every
event it receives, so the enforcement point for "no CV body in an event" is that adapter — and AC-13
is the test that guards it, asserting the field *set* of each event below rather than trusting this
docstring.

These are pure data with no behaviour: a dataclass field list *is* the whole signature, so unlike the
aggregate and the use case there is nothing to defer to GREEN.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    CvContentType,
    ExtractionFailureReason,
)
from tailorcraft.domain.shared.events import DomainEvent


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseCvUploaded(DomainEvent):
    """A base CV's bytes were accepted and stored, before extraction is attempted.

    Payload: `base_cv_id`, `guest_session_id`, `content_type`, `size_bytes` (+ inherited
    `occurred_at`). Deliberately absent: the original filename (PII) and the bytes themselves.
    """

    base_cv_id: BaseCvId
    guest_session_id: GuestSessionId
    content_type: CvContentType
    size_bytes: int


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseCvTextExtracted(DomainEvent):
    """Extraction succeeded.

    Payload: `base_cv_id`, `character_count` (+ inherited `occurred_at`). Deliberately absent: the
    extracted text itself — `character_count` exists on `ExtractedText` for exactly this reason, so
    that a caller reporting on the text never needs to hold the text.
    """

    base_cv_id: BaseCvId
    character_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseCvExtractionFailed(DomainEvent):
    """Extraction was decided as a failure rather than a success.

    Payload: `base_cv_id`, `reason` (+ inherited `occurred_at`). Deliberately absent: the library's
    own exception message, which can and does quote fragments of the file it failed to parse.
    """

    base_cv_id: BaseCvId
    reason: ExtractionFailureReason
