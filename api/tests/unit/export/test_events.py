"""Domain events for `export`: the five events' exact field sets (AC-33).

Pure domain tests: no I/O, no fixtures, no event loop, no mocks. Checked structurally via
`dataclasses.fields()`, the same form `tests/unit/tailoring/test_events.py` uses for the identical
reason: `LoggingEventPublisher` logs *every field of every event it receives*, so an event's field
set *is* a log field set, and a test that only greps for one forbidden string would pass vacuously
the day a new field nobody predicted (`markdown`, `html`, `rendered_bytes`, ...) is added.

Note on the event's name: the event is `ExportReady`, not `ExportJobReady` — the technical plan's
"## Domain events" table and `domain/export/events.py`'s own class name both say `ExportReady`; an
earlier draft of the feature spec's AC-33 said `ExportJobReady`, which was a spec typo, corrected
alongside the other four consistently-named events (`ExportRequested` / `ExportStarted` /
`ExportReady` / `ExportFailed`).
"""

from __future__ import annotations

import dataclasses

import pytest

from tailorcraft.domain.export.events import (
    DocumentRenderedInline,
    ExportFailed,
    ExportReady,
    ExportRequested,
    ExportStarted,
)
from tailorcraft.domain.shared.events import DomainEvent

# --- AC-33: the five exact field sets ------------------------------------------------------------


def test_export_requested_field_set_is_exactly_the_agreed_fields() -> None:
    """No text: the document this job will render is reachable from `tailoring_run_id` and
    `document` by anyone with database access and a reason, and it is not even copied onto the job
    row — carrying it here would be a third copy for 1.6 to purge."""
    field_names = {field.name for field in dataclasses.fields(ExportRequested)}

    assert field_names == {
        "export_job_id",
        "guest_session_id",
        "tailoring_run_id",
        "document",
        "format",
        "run_version",
        "occurred_at",
    }


def test_export_started_field_set_is_exactly_the_agreed_fields() -> None:
    """Deliberately sparse — this event's whole value is its timestamp, paired with
    `ExportRequested` (queue latency) and `ExportReady` (render latency)."""
    field_names = {field.name for field in dataclasses.fields(ExportStarted)}

    assert field_names == {"export_job_id", "occurred_at"}


def test_export_ready_field_set_is_exactly_the_agreed_fields() -> None:
    """No storage key: the key is a pure function of `export_job_id` and `format`, both already
    present, so carrying it too would be a derived field free to disagree with the row it was
    derived from. No bytes."""
    field_names = {field.name for field in dataclasses.fields(ExportReady)}

    assert field_names == {
        "export_job_id",
        "format",
        "byte_size",
        "render_duration_ms",
        "occurred_at",
    }


def test_export_failed_field_set_is_exactly_the_agreed_fields() -> None:
    """No renderer message: the closed `ExportFailureReason` enum is the only account of a failure
    an event ever carries — a free-text message is a field that can only ever leak."""
    field_names = {field.name for field in dataclasses.fields(ExportFailed)}

    assert field_names == {"export_job_id", "reason", "occurred_at"}


def test_document_rendered_inline_field_set_is_exactly_the_agreed_fields() -> None:
    """No text: on the inline path the rendered bytes are the entire response body, so this is the
    single easiest field in the slice to attach by accident — and the one this event is built to
    never carry."""
    field_names = {field.name for field in dataclasses.fields(DocumentRenderedInline)}

    assert field_names == {"tailoring_run_id", "document", "format", "byte_size", "occurred_at"}


@pytest.mark.parametrize(
    "event_type",
    [ExportRequested, ExportStarted, ExportReady, ExportFailed, DocumentRenderedInline],
    ids=["Requested", "Started", "Ready", "Failed", "RenderedInline"],
)
def test_no_event_field_set_contains_a_document_body_or_free_text_field(
    event_type: type[DomainEvent],
) -> None:
    """The disjointness form, on top of the five exact-set assertions above — the exact-set
    assertions catch *any* new field; this one names, for a human reading this file, exactly which
    strings would be the disaster if one showed up."""
    field_names = {field.name for field in dataclasses.fields(event_type)}

    forbidden_names = {
        "markdown",
        "html",
        "tokens",
        "rendered_bytes",
        "file_key",
        "key",
        "path",
        "cv_text",
        "cover_letter_text",
        "document_text",
        "extracted_text",
        "message",
        "error_message",
        "renderer_message",
    }

    assert field_names.isdisjoint(forbidden_names)
