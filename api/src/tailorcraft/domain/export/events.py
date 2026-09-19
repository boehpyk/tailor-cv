"""Domain events for the `export` bounded context.

Same rule as `intake`, `posting` and `tailoring`: an event carries **ids, enums, integers and
instants only** (AC-33). Explicitly absent from every event below — the document's Markdown, the
normalized tokens, the intermediate HTML, the rendered bytes, the storage key's path, the filename
and any renderer's own message.

The reason is mechanical rather than aspirational. `LoggingEventPublisher`
(`infrastructure/events/logging_publisher.py`) logs **every field of every event it receives**, so
an event's field set *is* a log field set: adding a field here is the same act as adding a column to
a log line. The text these events are about is a person's employment history rewritten — the densest
PII this product holds (Constitution §8) — and this is the slice where it finally becomes *bytes*,
which is the form somebody is most tempted to attach "just for debugging". AC-33 asserts the five
field sets below rather than trusting this docstring; a test is the only version of the rule that
can fail.

**Four lifecycle events, not one**, for 1.3's reason: requested, started, ready and failed are four
genuinely different facts occurring at four different times, and the gaps between them are the
product — `ExportReady` paired with `ExportRequested` is the queue latency, paired with
`ExportStarted` it is the render latency, and PRD §8's *> 98 % of exports succeed* is the ratio of
the last two. One `ExportJobStateChanged` with a status field would turn every subscriber into a
`match` and throw the timeline away.

These are pure data with no behaviour: a dataclass field list *is* the whole signature, so unlike
the aggregate there is nothing to defer to a GREEN step — this module is written whole at the
skeleton stage, exactly as `tailoring/events.py` and `posting/events.py` were.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.export.value_objects import (
    ExportFailureReason,
    ExportFormat,
    ExportJobId,
)
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportRequested(DomainEvent):
    """A visitor asked for one of a run's documents as a file: a job now exists, `queued`, and a
    task is about to be published for it.

    Payload: `export_job_id`, `guest_session_id`, `tailoring_run_id`, `document`, `format`,
    `run_version` (+ inherited `occurred_at`). **Deliberately absent: the text.** The document this
    job will render is reachable from `tailoring_run_id` and `document` by anyone with database
    access and a reason, which is the right bar; it does not belong in a log line, and it is not
    even copied onto the job row (a third copy of a CV is a third thing for 1.6 to purge).

    `run_version` is carried because it is the one field that makes this event answerable later:
    without it, "was this export stale when it was asked for?" cannot be reconstructed from the log
    at all.
    """

    export_job_id: ExportJobId
    guest_session_id: GuestSessionId
    tailoring_run_id: TailoringRunId
    document: TailoredDocumentKind
    format: ExportFormat
    run_version: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportStarted(DomainEvent):
    """A worker picked the job up and the render is about to begin.

    Payload: `export_job_id` (+ inherited `occurred_at`). Nothing else, and the sparseness is the
    point: this event's whole value is its *timestamp*. Paired with `ExportRequested` it is the
    queue-latency measurement; paired with `ExportReady` it is the wall-clock half of the render
    that `render_duration_ms` measures from inside. The owning session id is not repeated here — it
    is on the requested event for the same job id, and a field repeated on every event in a sequence
    is a field that eventually disagrees with itself.
    """

    export_job_id: ExportJobId


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportReady(DomainEvent):
    """The render produced bytes, the bytes are on the volume, and the job now owns a file.

    Payload: `export_job_id`, `format`, `byte_size`, `render_duration_ms` (+ inherited
    `occurred_at`). **This is the slice's success-rate and latency log line, by design** (PRD §8's
    > 98 %, AC-33): the two numbers are carried precisely so that nobody has to reach for a file to
    find out what a render cost.

    **Deliberately absent: the bytes, and the storage key.** The bytes are obvious. The key is the
    one worth explaining, because it holds no PII — it is a UUIDv7 and an extension, by
    construction (ADR-0011) — and it is still not this event's business: the key is a pure function
    of `export_job_id` and `format`, both of which are right here, so carrying it would be a
    derived field that a subscriber could one day find disagreeing with the row it was derived
    from. A log line that can contradict the database is worse than one that is merely quiet.
    """

    export_job_id: ExportJobId
    format: ExportFormat
    byte_size: int
    render_duration_ms: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportFailed(DomainEvent):
    """The job ended without a file, and this is the recorded reason.

    Payload: `export_job_id`, `reason` (+ inherited `occurred_at`). **Deliberately absent: the
    renderer's message and any part of the document.** A library's error string is written by people
    who assume nobody is watching what it quotes — the extraction sweep found `pypdf` messages
    quoting bytes out of the document that failed, and `weasyprint` logs CSS parse warnings with the
    offending declaration. The closed `ExportFailureReason` enum is what a dashboard groups by
    anyway; a free-text message is a field that can only ever leak.

    This event fires for all nine reasons, including the four nothing raises (`SOURCE_CHANGED`,
    `SOURCE_UNAVAILABLE`, `NOT_QUEUED`, `ABANDONED`): the aggregate records the fact of a failure,
    not the fact of an exception.
    """

    export_job_id: ExportJobId
    reason: ExportFailureReason


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentRenderedInline(DomainEvent):
    """A document was rendered to `md` or `txt` inside the request and handed straight back.

    Payload: `tailoring_run_id`, `document`, `format`, `byte_size` (+ inherited `occurred_at`).
    **Deliberately absent: the text**, which on this path is the entire response body and therefore
    the single easiest thing in the slice to attach by accident.

    **Recorded by no aggregate.** There is no `ExportJob` on the inline path — no row, no worker, no
    file (ADR-0005, ADR-0016 (a)) — so there is nothing to call `record` on and nothing to save,
    and `RenderDocumentInline` publishes this event **directly** after a successful render. That is
    a deliberate exception to the rule that events are released from an aggregate after a
    successful save, and it is safe for exactly the reason the rule exists: the rule protects
    against announcing a fact that a failed transaction is about to un-happen, and here there is no
    transaction to fail. The fact is *the bytes were produced and returned*, which is already true
    by the time this is published.

    It is the same shape ADR-0013 rejected for a *failed* fetch and it is right here, which is worth
    stating so neither reads as an accident: there, the argument was that a failure with no artifact
    should not manufacture a row; here, the event is the **cost-and-size log line for a path that
    has no row at all** — without it, half of this slice's traffic is invisible to the same
    dashboard that watches the other half.
    """

    tailoring_run_id: TailoringRunId
    document: TailoredDocumentKind
    format: ExportFormat
    byte_size: int
