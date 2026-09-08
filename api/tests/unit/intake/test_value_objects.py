"""Value objects for the `intake` context: `ExtractedText`, `OriginalFilename`, `CvContentType`.

Pure domain tests: no I/O, no fixtures, no event loop. Every assertion here comes from the
feature-spec's failure contract and the technical-plan's invariant table, not from running the
(currently unimplemented) code and recording what it did.
"""

from __future__ import annotations

import pytest

from tailorcraft.domain.intake.errors import EmptyExtraction, ExtractedTextTooShort, InvalidFilename
from tailorcraft.domain.intake.value_objects import ExtractedText, OriginalFilename

# --- ExtractedText -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty-string"),
        pytest.param("   ", id="spaces-only"),
        pytest.param("\n\t  \n", id="mixed-whitespace-only"),
    ],
)
def test_extracted_text_rejects_blank_content(value: str) -> None:
    """A blank or whitespace-only extraction is not "short text" — it is no text, and gets its own
    error so a caller can tell the two apart (e.g. to phrase `no_text_layer` differently from
    `too_short`)."""
    with pytest.raises(EmptyExtraction):
        ExtractedText(value)


def test_extracted_text_rejects_199_non_whitespace_characters() -> None:
    """One character short of the spec's floor (OQ-9: 200, provisional but chosen, not measured —
    this is the boundary the spec fixes, and the test must assert it independently of whatever the
    unimplemented code currently does)."""
    value = "a" * 199

    with pytest.raises(ExtractedTextTooShort):
        ExtractedText(value)


def test_extracted_text_accepts_exactly_200_non_whitespace_characters() -> None:
    """The floor itself is accepted, not just cleared — 200 is inclusive."""
    value = "a" * 200

    text = ExtractedText(value)

    assert text.character_count == 200


def test_extracted_text_character_count_reports_the_length() -> None:
    """`character_count` exists so a caller can report how much text there is without holding the
    text itself (Constitution §8 — the text is PII and must not cross the API response)."""
    value = "a" * 250

    text = ExtractedText(value)

    assert text.character_count == 250


# --- OriginalFilename ----------------------------------------------------------------------------


def test_original_filename_strips_a_posix_style_path() -> None:
    """A path-traversal attempt reduces to its basename — there is nowhere for the rest of the path
    to go, because `OriginalFilename` is never joined to a filesystem path (ADR-0011)."""
    name = OriginalFilename("../../etc/passwd")

    assert name.value == "passwd"


def test_original_filename_strips_a_windows_style_path() -> None:
    """The browser can send a Windows-style path regardless of the server's own platform, so the
    backslash must be treated as a separator unconditionally — not only when the host OS says so."""
    name = OriginalFilename("C:\\Users\\x\\cv.pdf")

    assert name.value == "cv.pdf"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("cv\x00.pdf", id="embedded-nul"),
        pytest.param("cv\x01report.pdf", id="control-character"),
        pytest.param("cv\x7f.pdf", id="delete-control-character"),
    ],
)
def test_original_filename_rejects_control_characters_and_nul(value: str) -> None:
    with pytest.raises(InvalidFilename):
        OriginalFilename(value)


def test_original_filename_rejects_more_than_255_characters_after_strip() -> None:
    """255 is the column width (`VARCHAR(255)`); one character over must be rejected here, at the
    value object, rather than truncated silently at the database."""
    value = "a" * 256

    with pytest.raises(InvalidFilename):
        OriginalFilename(value)


def test_original_filename_accepts_exactly_255_characters() -> None:
    value = "a" * 255

    name = OriginalFilename(value)

    assert name.value == value


def test_original_filename_rejects_a_string_that_is_empty_after_strip() -> None:
    """Whitespace the browser sent as a "filename" is not a display label for anything."""
    with pytest.raises(InvalidFilename):
        OriginalFilename("   ")


def test_original_filename_rejects_an_empty_string() -> None:
    with pytest.raises(InvalidFilename):
        OriginalFilename("")
