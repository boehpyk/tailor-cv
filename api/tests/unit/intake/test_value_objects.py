"""Value objects for the `intake` context: `ExtractedText`, `OriginalFilename`, `CvContentType`.

Pure domain tests: no I/O, no fixtures, no event loop. Every assertion here comes from the
feature-spec's failure contract and the technical-plan's invariant table, not from running the
(currently unimplemented) code and recording what it did.
"""

from __future__ import annotations

import pytest

from tailorcraft.domain.intake.errors import (
    EmptyExtraction,
    ExtractedTextTooShort,
    InvalidFilename,
    InvalidLabel,
)
from tailorcraft.domain.intake.value_objects import BaseCvLabel, ExtractedText, OriginalFilename

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


# --- ExtractedText keeps lines (ADR-0044, AC-1 to AC-3) ------------------------------------------

# Padding that clears the 200-character floor on its own, so a test about lines is never really a
# test about the floor. One line, no whitespace.
_PAD = "x" * 200


@pytest.mark.parametrize(
    "boundary",
    [
        pytest.param("\r\n", id="crlf"),
        pytest.param("\r", id="cr"),
        pytest.param("\x0b", id="vertical-tab"),
        pytest.param("\x0c", id="form-feed"),
        pytest.param("\x1c", id="file-separator"),
        pytest.param("\x1d", id="group-separator"),
        pytest.param("\x1e", id="record-separator"),
        pytest.param("\x85", id="next-line"),
        pytest.param("\u2028", id="line-separator"),
        pytest.param("\u2029", id="paragraph-separator"),
    ],
)
def test_extracted_text_turns_every_line_boundary_into_a_newline(boundary: str) -> None:
    """AC-1: every boundary `str.splitlines()` knows is one line break, written `\\n`."""
    text = ExtractedText(f"Jane Doe{boundary}Engineer{boundary}{_PAD}")

    assert text.value == f"Jane Doe\nEngineer\n{_PAD}"


def test_extracted_text_collapses_spaces_and_tabs_within_a_line() -> None:
    text = ExtractedText(f"Jane \t  Doe\t\tEngineer\n{_PAD}")

    assert text.value == f"Jane Doe Engineer\n{_PAD}"


def test_extracted_text_strips_each_line() -> None:
    text = ExtractedText(f"  Jane Doe  \n\t Engineer \t\n   {_PAD}   ")

    assert text.value == f"Jane Doe\nEngineer\n{_PAD}"


def test_extracted_text_collapses_a_run_of_blank_lines_to_one() -> None:
    text = ExtractedText(f"Experience\n\n\n\n\nAcme\n \t \n \n\n{_PAD}")

    assert text.value == f"Experience\n\nAcme\n\n{_PAD}"


def test_extracted_text_keeps_a_single_blank_line_between_blocks() -> None:
    text = ExtractedText(f"Experience\n\nAcme\n{_PAD}")

    assert text.value == f"Experience\n\nAcme\n{_PAD}"


def test_extracted_text_drops_leading_and_trailing_blank_lines() -> None:
    text = ExtractedText(f"\n \n\n{_PAD}\nEnd\n\n \n")

    assert text.value == f"{_PAD}\nEnd"


def test_extracted_text_keeps_markdown_markers_and_their_lines() -> None:
    """A DOCX arrives with `#`, `**` and `- `; only whitespace is normalized, never markup."""
    text = ExtractedText(f"# Jane Doe\n**Engineer**\n\n- Led   a team\n- {_PAD}")

    assert text.value == f"# Jane Doe\n**Engineer**\n\n- Led a team\n- {_PAD}"


def test_extracted_text_leaves_an_old_one_line_value_unchanged() -> None:
    """AC-3: a row stored by the flattening version loads as it was stored."""
    old = "Jane Doe Engineer " + "word " * 60
    old = old.strip()

    assert ExtractedText(old).value == old


def test_extracted_text_normalization_is_idempotent() -> None:
    """AC-3: the repository rebuilds `ExtractedText` from the stored value on every load."""
    once = ExtractedText(f" Jane \t Doe \r\n\r\n\r\n\x0b  Engineer\n\n{_PAD}  \n\n")

    assert ExtractedText(once.value).value == once.value


def test_extracted_text_accepts_200_non_whitespace_characters_split_over_lines() -> None:
    """AC-2: the floor counts non-whitespace only; a newline is not a character that counts."""
    value = "a" * 100 + "\n\n" + "b" * 50 + "\n" + "c" * 50

    assert ExtractedText(value).value == value


def test_extracted_text_rejects_199_non_whitespace_characters_split_over_lines() -> None:
    """The same 199 characters, with newlines between them, are still too short."""
    value = "a" * 100 + "\n\n" + "b" * 50 + "\n" + "c" * 49

    with pytest.raises(ExtractedTextTooShort):
        ExtractedText(value)


def test_extracted_text_too_short_message_counts_non_whitespace_only() -> None:
    """The error's number must not count the newlines (AC-2: errors unchanged)."""
    with pytest.raises(ExtractedTextTooShort, match="only 199 non-whitespace"):
        ExtractedText("a" * 100 + "\n\n" + "b" * 99)


@pytest.mark.parametrize("value", [pytest.param("\r\n\x0b\x0c\u2028 \t", id="line-boundaries")])
def test_extracted_text_rejects_blank_content_made_of_line_boundaries(value: str) -> None:
    with pytest.raises(EmptyExtraction):
        ExtractedText(value)


def test_extracted_text_character_count_counts_newlines() -> None:
    """`character_count` is the length of the normalized text, newlines included (AC-2)."""
    text = ExtractedText(f"Jane\n\n{_PAD}")

    assert text.character_count == len("Jane\n\n") + 200


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


# --- BaseCvLabel (AC-4, slice 2.2) ---------------------------------------------------------------
#
# A label is a display string a registered user gives a saved base CV — 1 to 80 code points after
# `strip()`, refusing control characters and NUL, exactly as `OriginalFilename` refuses them for the
# same reason: a label is text for a screen, never a path and never markup. Unlike
# `OriginalFilename` there is no basename reduction — a label was never a filename to begin with.


def test_base_cv_label_strips_surrounding_whitespace() -> None:
    label = BaseCvLabel("  Senior Backend Role  ")

    assert label.value == "Senior Backend Role"


def test_base_cv_label_rejects_a_string_that_is_empty_after_strip() -> None:
    with pytest.raises(InvalidLabel):
        BaseCvLabel("   ")


def test_base_cv_label_rejects_an_empty_string() -> None:
    with pytest.raises(InvalidLabel):
        BaseCvLabel("")


def test_base_cv_label_rejects_more_than_80_code_points_after_strip() -> None:
    value = "a" * 81

    with pytest.raises(InvalidLabel):
        BaseCvLabel(value)


def test_base_cv_label_accepts_exactly_80_code_points() -> None:
    value = "a" * 80

    label = BaseCvLabel(value)

    assert label.value == value


def test_base_cv_label_accepts_exactly_1_code_point() -> None:
    label = BaseCvLabel("x")

    assert label.value == "x"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("cv\x00label", id="embedded-nul"),
        pytest.param("cv\x01label", id="control-character"),
        pytest.param("cv\x7flabel", id="delete-control-character"),
        pytest.param("first line\nsecond line", id="embedded-newline"),
    ],
)
def test_base_cv_label_rejects_control_characters_and_nul(value: str) -> None:
    with pytest.raises(InvalidLabel):
        BaseCvLabel(value)


def test_base_cv_label_equality_by_value() -> None:
    """A label is a plain value object like every other in this module — compared structurally, not
    by identity, so two labels typed the same way are interchangeable."""
    assert BaseCvLabel("My CV") == BaseCvLabel("My CV")
    assert BaseCvLabel("My CV") != BaseCvLabel("Other CV")
