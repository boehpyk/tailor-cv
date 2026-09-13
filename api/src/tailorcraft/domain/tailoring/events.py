"""Domain events for the `tailoring` bounded context.

Same rule as `domain/intake/events.py` and `domain/posting/events.py`, and this is the context where
it bites hardest: an event carries **ids, enums, counts and metrics only**. Explicitly absent from
every event below — a tailored CV, a cover letter, the base CV's extracted text, the job posting's
text, the assembled prompt, the model's completion, and the provider's own error message.

The reason is mechanical rather than aspirational. `LoggingEventPublisher`
(`infrastructure/events/logging_publisher.py`) logs **every field of every event it receives**, so an
event's field set *is* a log field set: adding a field here is the same act as adding a column to a
log line, and it should be made with that in mind rather than as a convenience for one subscriber. A
tailored CV is a person's employment history rewritten — the densest PII this product ever holds
(Constitution §8) — and it would take exactly one convenient field to put a stranger's address into a
file nobody thinks of as a database. 1.3's AC-22 asserts the four run-lifecycle field sets below,
and 1.4's AC-20 asserts the fifth, rather than trusting this docstring — a test is the only version
of the rule that can actually fail.

**`TailoringRunSucceeded` is this slice's cost-and-latency log line, by design.** Publishing it is
how the 15-second budget (Constitution §7) and the token spend become observable at all: the five
metric fields are carried precisely so that nobody has to reach into a response object to find out
what a call cost. That is the payoff for modelling the numbers as `LlmCallMetrics` — a named value
object with no prompt and no completion in it — instead of as a print statement next to the SDK call.

**Four lifecycle events, not one.** `JobPostingCaptured` deliberately went the other way, folding
`source` into a single fact, and the contrast is worth stating so neither one reads as an accident.
There, "pasted" and "fetched" were two ways one thing happened at one moment, so `source` was a
*field of one fact*. Here there are four genuinely different facts occurring at four different times
— requested, started, succeeded, failed — and the gaps between them are the product: 1.4's progress
display renders the ordering, and 2.3's history reads it back. Collapsing them into one
`TailoringRunStateChanged` would turn every subscriber into a `match` over a status field and throw
away the timeline. The fifth event, `TailoredDocumentRevised`, applies the same test and lands on the
1.2 side of it: one fact with a `kind` field, not two events — its docstring says why.

These are pure data with no behaviour: a dataclass field list *is* the whole signature, so unlike the
aggregate there is nothing to defer to a GREEN step — this module is written whole at the skeleton
stage (T4), exactly as `posting/events.py` was.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.domain.tailoring.value_objects import (
    ModelName,
    PromptVersion,
    TailoredDocumentKind,
    TailoringFailureReason,
    TailoringRunId,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class TailoringRunRequested(DomainEvent):
    """A visitor asked for two documents: a run now exists, `queued`, and a task is about to be
    published for it.

    Payload: `tailoring_run_id`, `guest_session_id`, `base_cv_id`, `job_posting_id` (+ inherited
    `occurred_at`). **Deliberately absent: everything textual.** The four ids are the whole fact —
    what was asked for, on whose behalf, from which two inputs. The CV's filename, the posting's URL
    and the posting's title are all reachable from those ids by anyone with database access and a
    reason, which is the right bar; none of them belongs in a log line (the same call 1.1 made about
    `BaseCvUploaded` and 1.2 made about `JobPostingCaptured`).
    """

    tailoring_run_id: TailoringRunId
    guest_session_id: GuestSessionId
    base_cv_id: BaseCvId
    job_posting_id: JobPostingId


@dataclass(frozen=True, slots=True, kw_only=True)
class TailoringRunStarted(DomainEvent):
    """A worker picked the run up and the call to the model is about to be made.

    Payload: `tailoring_run_id` (+ inherited `occurred_at`). Nothing else, and the sparseness is the
    point: this event's whole value is its *timestamp*. Paired with `TailoringRunRequested` it is the
    queue-latency measurement (how long a run waited for a worker), and paired with
    `TailoringRunSucceeded` it is the wall-clock half of the 15-second budget that `duration_ms` does
    not cover. The owning session id is not repeated here — it is on the requested event for the same
    run id, and a field repeated on every event in a sequence is a field that eventually disagrees
    with itself.
    """

    tailoring_run_id: TailoringRunId


@dataclass(frozen=True, slots=True, kw_only=True)
class TailoringRunSucceeded(DomainEvent):
    """The model answered, the answer re-validated, and the run now owns two documents.

    Payload: `tailoring_run_id`, `model`, `prompt_version`, `prompt_tokens`, `completion_tokens`,
    `duration_ms`, `cv_character_count`, `cover_letter_character_count` (+ inherited `occurred_at`).

    **Deliberately absent: the CV, the letter, the prompt and the completion.** The two character
    counts exist on `TailoredCv` and `CoverLetter` for exactly this reason — so that a subscriber
    reporting on how much text was produced never needs to hold the text. A count in a log line is a
    size; the string it counts is somebody's career.

    The five metric fields are flattened out of `LlmCallMetrics` rather than carried as one nested
    value object, and that is a small deliberate choice: `LoggingEventPublisher` logs fields, so a
    nested dataclass would arrive in the log as one opaque `repr` that no log query can aggregate
    over. `model` and `prompt_version` keep their value-object types (they are the provenance keys
    2.3's history and every prompt regression will group by); the three integers are counts and
    milliseconds and have nothing to validate at this point.
    """

    tailoring_run_id: TailoringRunId
    model: ModelName
    prompt_version: PromptVersion
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    cv_character_count: int
    cover_letter_character_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class TailoringRunFailed(DomainEvent):
    """The run ended without two documents, and this is the recorded reason.

    Payload: `tailoring_run_id`, `reason` (+ inherited `occurred_at`). **Deliberately absent: the
    provider's message, the raw response, and any input text.** A vendor's error string is written by
    people who assume nobody is watching what it quotes — the extraction sweep found `pypdf` messages
    quoting bytes out of the document that failed — and here the input is a CV. The closed
    `TailoringFailureReason` enum is what a dashboard groups by anyway; a free-text message is a field
    that can only ever leak.

    Note that this event fires for all nine reasons, including the two nothing raises (`NOT_QUEUED`,
    `ABANDONED`): the aggregate records the fact of a failure, not the fact of an exception.
    """

    tailoring_run_id: TailoringRunId
    reason: TailoringFailureReason


@dataclass(frozen=True, slots=True, kw_only=True)
class TailoredDocumentRevised(DomainEvent):
    """The user replaced one of a succeeded run's documents with their own revision (slice 1.4).

    Payload: `tailoring_run_id`, `kind`, `version`, `character_count` (+ inherited `occurred_at`).
    **Deliberately absent: the text, the previous text, and any diff between them** (AC-20,
    Constitution §8). A revision is a person's own words about their own career — the one place in
    this product where the PII is typed by the subject rather than extracted from a file — and it
    would take exactly one "helpful" `before`/`after` pair to put it into every log line
    `LoggingEventPublisher` writes. `character_count` is the size; the string it counts stays on the
    row. `version` is the number *after* the bump, so a subscriber can order two revisions of one
    run without a clock.

    **One event for both kinds, with `kind` as a field.** This is the 1.2 shape (`JobPostingCaptured`
    with `source`), not the 1.3 shape above (four events for four different facts), and the choice
    is the same test applied honestly: there is a single fact here — *a document of this run was
    revised* — occurring for one of two documents at one moment. `TailoredCvRevised` and
    `CoverLetterRevised` would be two names for one fact, and every future subscriber (1.5's export
    cache first) would have to handle both and could forget the second. The four 1.3 events stay
    four because *requested*, *started*, *succeeded* and *failed* are different facts at different
    times whose gaps are the product; this one is one fact with a discriminator.
    """

    tailoring_run_id: TailoringRunId
    kind: TailoredDocumentKind
    version: int
    character_count: int
