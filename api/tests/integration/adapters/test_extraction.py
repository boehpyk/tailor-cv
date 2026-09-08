"""Adapter tests for `PypdfDocxTextExtractor` (`CvTextExtractorPort`), written **after** (T29):
real extraction from the happy-path corpus, the failure-contract translation for the known-bad
fixtures, and the 50-page cap behind the `slow` marker (technical-plan.md's Adapters row; F-6…F-11).

**OQ-9** — the 200-non-whitespace-character floor on `ExtractedText` is documented as provisional,
"chosen, not measured" (technical-plan.md, `domain/intake/value_objects.py`). `test_oq9_*` below
measures what the committed corpus actually produces and asserts *properties* against the floor
(comfortably above it / comfortably below it) rather than an exact character count copied from a
run of the code — CLAUDE.md's rule that a test must encode what the code should do, not what it was
observed doing, applies especially hard to a test investigating a number the code itself calls
unmeasured. The measured numbers are reported in this module's own docstring rather than silently
adjusting the floor: `sample.{pdf,docx,txt}` (a realistic one-page CV body) extract to 867
non-whitespace characters — more than 4x the 200 floor — and `tiny.txt` (chosen to be "too short")
extracts to 39, comfortably below it. Nothing in the corpus contradicts 200 as a floor: it does not
falsely reject the realistic sample, and it does correctly reject the deliberately-too-short one. The
corpus does not, however, contain a fixture *near* the boundary (150-250 non-whitespace characters),
so this run cannot confirm 200 is well-calibrated at the edge — only that it is not obviously wrong
for the cases the corpus covers. **Recommendation: keep 200 unchanged** on the evidence available;
a boundary-straddling fixture would be a good future addition to the corpus (README.md already
invites exactly that kind of growth) if OQ-9 needs a tighter answer later.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from pypdf import PdfWriter

from tailorcraft.domain.intake.errors import (
    CorruptCvFile,
    CvExtractionFailed,
    CvHasNoTextLayer,
    CvHasTooManyPages,
    CvTextTooShort,
    EncryptedCvFile,
)
from tailorcraft.domain.intake.value_objects import CvContentType, ExtractedText
from tailorcraft.infrastructure.intake.extraction import PypdfDocxTextExtractor

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "cvs"

# `sample.{pdf,docx,txt}` share this body (tests/fixtures/cvs/README.md): known phrases the
# extracted text must contain. Asserting a *property* the fixture's author put there on purpose,
# rather than an exact string copied from a run of the extractor.
_KNOWN_NAME = "Alex Rivera"
_KNOWN_EMPLOYER = "Northwind Logistics"


def _read_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def _extractor() -> PypdfDocxTextExtractor:
    return PypdfDocxTextExtractor(timeout_seconds=10, max_pages=50)


# --- Happy path: real text out of the corpus ------------------------------------------------------

_HAPPY_PATH_CASES = [
    pytest.param("sample.pdf", CvContentType.PDF, id="sample.pdf"),
    pytest.param("sample.docx", CvContentType.DOCX, id="sample.docx"),
    pytest.param("sample.txt", CvContentType.TXT, id="sample.txt"),
]


@pytest.mark.parametrize(("fixture_name", "content_type"), _HAPPY_PATH_CASES)
async def test_extracts_real_text_containing_known_phrases(
    fixture_name: str, content_type: CvContentType
) -> None:
    data = _read_fixture(fixture_name)

    result = await _extractor().extract(content_type, data)

    assert isinstance(result, ExtractedText)
    assert result.character_count >= 200
    assert _KNOWN_NAME in result.value
    assert _KNOWN_EMPLOYER in result.value


# --- Failure-contract translation for the known-bad fixtures ---------------------------------------

_FAILURE_CASES = [
    pytest.param("encrypted.pdf", CvContentType.PDF, EncryptedCvFile, id="F-7-encrypted"),
    pytest.param("corrupt.pdf", CvContentType.PDF, CorruptCvFile, id="F-8-corrupt"),
    pytest.param("scanned.pdf", CvContentType.PDF, CvHasNoTextLayer, id="F-9-no_text_layer"),
    pytest.param("tiny.txt", CvContentType.TXT, CvTextTooShort, id="F-10-too_short"),
]


@pytest.mark.parametrize(("fixture_name", "content_type", "expected_exc"), _FAILURE_CASES)
async def test_translates_known_bad_fixtures_to_the_matching_failure(
    fixture_name: str, content_type: CvContentType, expected_exc: type[CvExtractionFailed]
) -> None:
    data = _read_fixture(fixture_name)

    with pytest.raises(expected_exc):
        await _extractor().extract(content_type, data)


# --- The 50-page cap, checked before any page's content is parsed ----------------------------------


@pytest.mark.slow
async def test_a_pdf_over_the_page_cap_is_refused_before_any_page_is_parsed() -> None:
    writer = PdfWriter()
    for _ in range(51):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)

    with pytest.raises(CvHasTooManyPages):
        await _extractor().extract(CvContentType.PDF, buffer.getvalue())


# --- OQ-9: the 200-character floor, measured against the corpus ------------------------------------


@pytest.mark.parametrize(
    ("fixture_name", "content_type"),
    [
        pytest.param("sample.pdf", CvContentType.PDF, id="sample.pdf"),
        pytest.param("sample.docx", CvContentType.DOCX, id="sample.docx"),
        pytest.param("sample.txt", CvContentType.TXT, id="sample.txt"),
    ],
)
async def test_oq9_a_realistic_one_page_cv_clears_the_200_character_floor_by_a_wide_margin(
    fixture_name: str, content_type: CvContentType
) -> None:
    """A property assertion, not the exact copied count (module docstring): a realistic one-page CV
    body must land well clear of the floor, not just barely over it — if it did not, 200 would be
    too aggressive for ordinary content and this test would be the place that says so."""
    data = _read_fixture(fixture_name)

    result = await _extractor().extract(content_type, data)

    # more than 4x the floor on the measured corpus (867 non-whitespace characters) — a wide margin
    # a small future change to `sample.*`'s body should not accidentally erode to nothing.
    assert result.character_count >= 400
