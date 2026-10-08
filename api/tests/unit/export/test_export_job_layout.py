"""Slice 3.2, AC-1...AC-4: the layout template on the export domain.

Pure domain tests, written from the spec (feature-spec.md AC-1...AC-4, technical-plan.md section
0.3) before the refusals and `takes_layout_template` exist. Refusal order: inline format, then a PDF
with no layout, then a layout on a non-PDF, then the run version.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from tailorcraft.domain.export.errors import (
    ExportFormatNotQueued,
    InvalidRunVersion,
    LayoutTemplateNotApplicable,
    LayoutTemplateRequired,
)
from tailorcraft.domain.export.events import (
    ExportFailed,
    ExportReady,
    ExportRequested,
    ExportStarted,
)
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import (
    DEFAULT_LAYOUT_TEMPLATE,
    ExportFailureReason,
    ExportFormat,
    ExportJobId,
    LayoutTemplate,
)
from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId

_AT = datetime(2026, 10, 8, 10, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 10, 8, 10, 0, 30, tzinfo=UTC)


def _request(
    format: ExportFormat,
    layout_template: LayoutTemplate | None,
    run_version: int = 1,
) -> ExportJob:
    return ExportJob.request(
        id=ExportJobId(value=UUID("0192f0a1-89ab-7cde-8123-456789abcde0")),
        owner=GuestOwner(GuestSessionId(value=UUID("11111111-1111-7111-8111-111111111111"))),
        tailoring_run_id=TailoringRunId(value=UUID("33333333-3333-7333-8333-333333333333")),
        document=TailoredDocumentKind.CV,
        format=format,
        layout_template=layout_template,
        run_version=run_version,
        requested_at=_AT,
    )


@pytest.fixture
def constructions(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Records every `ExportJob()` built, so "refused before `cls()`" is observable: a refusal that
    built an instance and then raised leaves an entry here."""
    built: list[object] = []
    original = ExportJob.__init__

    def spy(self: ExportJob, *args: Any, **kwargs: Any) -> None:
        built.append(self)
        original(self, *args, **kwargs)

    monkeypatch.setattr(ExportJob, "__init__", spy)
    return built


# --- AC-1: the closed set ----------------------------------------------------------------------


def test_layout_templates_are_exactly_classic_modern_formal_in_display_order() -> None:
    assert [m.value for m in LayoutTemplate] == ["classic", "modern", "formal"], (
        "LayoutTemplate members changed. NEVER delete a member: export rows still hold it and "
        "must load (ADR-0030's retirement rule keeps the member and refuses it at the request "
        "path). Adding one means updating the client mirror "
        "web/src/features/export/layouts.ts (AC-29) in the same change."
    )


def test_default_layout_template_is_classic() -> None:
    assert DEFAULT_LAYOUT_TEMPLATE is LayoutTemplate.CLASSIC


# --- AC-2: which formats take a layout ---------------------------------------------------------


@pytest.mark.parametrize(
    ("format", "expected"),
    [
        (ExportFormat.PDF, True),
        (ExportFormat.DOCX, False),
        (ExportFormat.MD, False),
        (ExportFormat.TXT, False),
    ],
)
def test_only_pdf_takes_a_layout_template(format: ExportFormat, expected: bool) -> None:
    assert format.takes_layout_template is expected


# --- AC-3: the refusals, in order, before cls() ------------------------------------------------


def test_a_pdf_with_no_layout_is_refused_as_required(constructions: list[object]) -> None:
    with pytest.raises(LayoutTemplateRequired) as raised:
        _request(ExportFormat.PDF, None)

    assert type(raised.value) is LayoutTemplateRequired
    assert constructions == []


@pytest.mark.parametrize("layout", list(LayoutTemplate))
def test_a_docx_with_a_layout_is_refused_as_not_applicable(
    layout: LayoutTemplate, constructions: list[object]
) -> None:
    with pytest.raises(LayoutTemplateNotApplicable) as raised:
        _request(ExportFormat.DOCX, layout)

    assert type(raised.value) is LayoutTemplateNotApplicable
    assert raised.value.format is ExportFormat.DOCX
    assert constructions == []


@pytest.mark.parametrize("format", [ExportFormat.MD, ExportFormat.TXT])
@pytest.mark.parametrize("layout", [None, LayoutTemplate.CLASSIC])
def test_an_inline_format_is_refused_as_not_queued_before_the_layout_is_judged(
    format: ExportFormat, layout: LayoutTemplate | None, constructions: list[object]
) -> None:
    with pytest.raises(ExportFormatNotQueued) as raised:
        _request(format, layout)

    assert type(raised.value) is ExportFormatNotQueued
    assert constructions == []


def test_a_missing_layout_is_judged_before_the_run_version(
    constructions: list[object],
) -> None:
    with pytest.raises(LayoutTemplateRequired) as raised:
        _request(ExportFormat.PDF, None, run_version=0)

    assert type(raised.value) is LayoutTemplateRequired
    assert constructions == []


def test_an_inapplicable_layout_is_judged_before_the_run_version(
    constructions: list[object],
) -> None:
    with pytest.raises(LayoutTemplateNotApplicable) as raised:
        _request(ExportFormat.DOCX, LayoutTemplate.MODERN, run_version=0)

    assert type(raised.value) is LayoutTemplateNotApplicable
    assert constructions == []


def test_the_run_version_is_still_refused_when_the_layout_is_right() -> None:
    with pytest.raises(InvalidRunVersion) as raised:
        _request(ExportFormat.PDF, LayoutTemplate.CLASSIC, run_version=0)

    assert type(raised.value) is InvalidRunVersion


def test_positive_control_valid_requests_do_construct_and_record_one_event(
    constructions: list[object],
) -> None:
    pdf = _request(ExportFormat.PDF, LayoutTemplate.FORMAL)
    docx = _request(ExportFormat.DOCX, None)

    assert constructions == [pdf, docx]
    assert len(pdf.release_events()) == 1
    assert len(docx.release_events()) == 1


# --- AC-4: the property, the event field, no transition touches it ------------------------------


@pytest.mark.parametrize("layout", list(LayoutTemplate))
def test_a_pdf_job_returns_the_layout_it_was_requested_with(layout: LayoutTemplate) -> None:
    assert _request(ExportFormat.PDF, layout).layout_template is layout


def test_a_docx_job_has_no_layout() -> None:
    assert _request(ExportFormat.DOCX, None).layout_template is None


def test_export_requested_carries_the_layout() -> None:
    (event,) = _request(ExportFormat.PDF, LayoutTemplate.MODERN).release_events()

    assert isinstance(event, ExportRequested)
    assert event.layout_template is LayoutTemplate.MODERN


def test_export_requested_for_docx_carries_none() -> None:
    (event,) = _request(ExportFormat.DOCX, None).release_events()

    assert isinstance(event, ExportRequested)
    assert event.layout_template is None


def test_no_other_export_event_has_a_layout_field() -> None:
    for event_type in (ExportStarted, ExportReady, ExportFailed):
        assert "layout_template" not in event_type.__dataclass_fields__, event_type.__name__


def test_the_layout_cannot_be_assigned() -> None:
    job = _request(ExportFormat.PDF, LayoutTemplate.MODERN)

    with pytest.raises(AttributeError):
        job.layout_template = LayoutTemplate.FORMAL  # type: ignore[misc]

    assert job.layout_template is LayoutTemplate.MODERN


def test_no_transition_changes_the_layout() -> None:
    ready = _request(ExportFormat.PDF, LayoutTemplate.FORMAL)
    ready.mark_started(_AT)
    assert ready.layout_template is LayoutTemplate.FORMAL
    ready.mark_ready(byte_size=10, render_duration_ms=5, at=_LATER)
    assert ready.layout_template is LayoutTemplate.FORMAL

    failed = _request(ExportFormat.PDF, LayoutTemplate.MODERN)
    failed.mark_failed(ExportFailureReason.RENDER_FAILED, _LATER)
    assert failed.layout_template is LayoutTemplate.MODERN
