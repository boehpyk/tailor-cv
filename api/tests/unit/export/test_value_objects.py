"""Value objects for the `export` context: `ExportFormat`'s twelve facts (AC-1), `download_filename`'s
eight constants (AC-27's domain half), and `FileRef.for_export`'s shape and refusal (AC-7).

Pure domain tests: no I/O, no fixtures, no event loop, no mocks. Every expected value here is copied
from the acceptance criteria and the technical plan, never from running the (currently
`NotImplementedError`) code and recording what it did.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from tailorcraft.domain.export.errors import ExportFormatNotQueued
from tailorcraft.domain.export.value_objects import (
    ExportDelivery,
    ExportFormat,
    ExportJobId,
    download_filename,
)
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind

# --- AC-1: ExportFormat's twelve facts, table-driven -------------------------------------------


@pytest.mark.parametrize(
    ("format", "expected_delivery", "expected_media_type", "expected_extension"),
    [
        pytest.param(
            ExportFormat.MD,
            ExportDelivery.INLINE,
            "text/markdown; charset=utf-8",
            "md",
            id="md",
        ),
        pytest.param(
            ExportFormat.TXT,
            ExportDelivery.INLINE,
            "text/plain; charset=utf-8",
            "txt",
            id="txt",
        ),
        pytest.param(
            ExportFormat.PDF,
            ExportDelivery.QUEUED,
            "application/pdf",
            "pdf",
            id="pdf",
        ),
        pytest.param(
            ExportFormat.DOCX,
            ExportDelivery.QUEUED,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
            id="docx",
        ),
    ],
)
def test_export_format_facts(
    format: ExportFormat,
    expected_delivery: ExportDelivery,
    expected_media_type: str,
    expected_extension: str,
) -> None:
    """AC-1: each of the four members carries its own `delivery`, `media_type` and
    `file_extension` — twelve facts total, none re-derived anywhere outside this type."""
    assert format.delivery is expected_delivery
    assert format.media_type == expected_media_type
    assert format.file_extension == expected_extension


# --- AC-27 (domain half): download_filename's eight constants ----------------------------------


@pytest.mark.parametrize(
    ("document", "format", "expected_filename"),
    [
        pytest.param(TailoredDocumentKind.CV, ExportFormat.MD, "tailored-cv.md", id="cv-md"),
        pytest.param(TailoredDocumentKind.CV, ExportFormat.TXT, "tailored-cv.txt", id="cv-txt"),
        pytest.param(TailoredDocumentKind.CV, ExportFormat.PDF, "tailored-cv.pdf", id="cv-pdf"),
        pytest.param(TailoredDocumentKind.CV, ExportFormat.DOCX, "tailored-cv.docx", id="cv-docx"),
        pytest.param(
            TailoredDocumentKind.COVER_LETTER,
            ExportFormat.MD,
            "cover-letter.md",
            id="cover-letter-md",
        ),
        pytest.param(
            TailoredDocumentKind.COVER_LETTER,
            ExportFormat.TXT,
            "cover-letter.txt",
            id="cover-letter-txt",
        ),
        pytest.param(
            TailoredDocumentKind.COVER_LETTER,
            ExportFormat.PDF,
            "cover-letter.pdf",
            id="cover-letter-pdf",
        ),
        pytest.param(
            TailoredDocumentKind.COVER_LETTER,
            ExportFormat.DOCX,
            "cover-letter.docx",
            id="cover-letter-docx",
        ),
    ],
)
def test_download_filename_is_a_constant_per_document_and_format(
    document: TailoredDocumentKind, format: ExportFormat, expected_filename: str
) -> None:
    """AC-27's domain half: the filename never derives from user text, only from the (document,
    format) pair — one of eight fixed constants."""
    assert download_filename(document, format) == expected_filename


# --- AC-7: FileRef.for_export's shape and its refusal of an inline format ----------------------

# UUID chosen so the hand-computed key below can be checked by eye, the same UUID
# `test_for_base_cv_matches_a_hand_computed_key` uses for the identical reason.
# hex (no dashes): 0192f0a189ab7cde8123456789abcdef -> hex[0:2] = "01", hex[2:4] = "92"
_JOB_ID = ExportJobId(value=UUID("0192f0a1-89ab-7cde-8123-456789abcdef"))


def test_for_export_matches_a_hand_computed_key_for_pdf() -> None:
    ref = FileRef.for_export(_JOB_ID, ExportFormat.PDF)

    assert ref.key == "01/92/0192f0a1-89ab-7cde-8123-456789abcdef.pdf"


def test_for_export_matches_a_hand_computed_key_for_docx() -> None:
    ref = FileRef.for_export(_JOB_ID, ExportFormat.DOCX)

    assert ref.key == "01/92/0192f0a1-89ab-7cde-8123-456789abcdef.docx"


def test_for_export_is_deterministic() -> None:
    """Same job id, same format, same key — every time (idempotent write, XJ-7)."""
    first = FileRef.for_export(_JOB_ID, ExportFormat.PDF)
    second = FileRef.for_export(_JOB_ID, ExportFormat.PDF)

    assert first == second


@pytest.mark.parametrize(
    "format",
    [ExportFormat.MD, ExportFormat.TXT],
    ids=["md", "txt"],
)
def test_for_export_refuses_an_inline_format(format: ExportFormat) -> None:
    """An inline format has no file and therefore no ref (AC-7): `md` and `txt` are never stored,
    and the grammar is not widened to admit them."""
    with pytest.raises(ExportFormatNotQueued):
        FileRef.for_export(_JOB_ID, format)
