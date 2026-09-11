"""`parse_tailoring_response` — the re-validation table (AC-7, T23), the highest-value test in this
slice.

Pure: no SDK, no HTTP, no clock, no database, no event loop, no mocks. Every fixture below is a raw
string, exactly the shape the SDK could hand back — possibly fenced, possibly wrapped in prose,
possibly truncated, possibly not JSON at all — and every assertion comes from the feature spec's
failure contract (AC-7, G-20, G-21) and `infrastructure/llm/parsing.py`'s module docstring, which
resolves four ambiguities the table alone leaves open and is authoritative about all of them. In
particular: an empty-string document is `document_too_short`, not a distinct label (the label
vocabulary is closed at seven and blank is the limiting case of below-the-floor), and a JSON `null`
is `wrong_type`, not `missing_field` (the key is present; the value is not a `str`).

**Not from running the code.** At the time of writing, `parse_tailoring_response` raises
`NotImplementedError` unconditionally (docs/sdlc.md §2) — T24 fills in the body this table
specifies. The floors and ceilings (400/20,000 for `TailoredCv`, 200/8,000 for `CoverLetter`) are
hard-coded here rather than imported from `domain/tailoring/value_objects.py`'s private module
constants, for the reason `test_value_objects.py` already documents: a test that imports the number
it is checking cannot disagree with the code. The `PROBLEM_*` label strings are hard-coded for the
same reason, even though `parsing.py` exports them as constants — the vocabulary is fixed by the
spec's own text (feature-spec row G-20 names `missing_field`, `wrong_type`, `not_json`,
`document_too_short` verbatim), not by whatever the module happens to call them today.

**The assertion that matters most is not about labels.** AC-7/G-20: "never the raw response — it
contains a half-written CV." Every failing-row fixture below embeds a distinctive sentinel — a fake
name plus a unique token that could not occur by accident — in whichever field(s) the fixture
carries, and `test_failing_rows_never_leak_the_raw_response_into_the_exception` asserts the sentinel
is absent from both `str(exc)` and `repr(exc)` for **every** row in the table, not as one extra test
bolted on the side. That is deliberate: a per-row check is what catches a future label that
"helpfully" interpolates the offending value it rejected. The one row where a sentinel cannot be
embedded (the bare top-level JSON number) is noted at its definition — the leak assertion there is
vacuously true, and every other row carries the weight.
"""

from __future__ import annotations

import json

import pytest

from tailorcraft.domain.tailoring.errors import LlmOutputInvalid
from tailorcraft.domain.tailoring.value_objects import CoverLetter, TailoredCv, TailoredDocuments
from tailorcraft.infrastructure.llm.parsing import parse_tailoring_response

# A fake name plus a unique token: distinctive enough that it could not appear in a real fixture or
# an exception message by accident, so its absence from `str(exc)`/`repr(exc)` is a meaningful
# assertion rather than a coincidence.
_SENTINEL = "Zbigniew Kowalczyk (ref 778f1c92e4b1)"


def _cv_text(filler: int = 500) -> str:
    """Valid `TailoredCv` text: well past the 400-character floor, nowhere near the 20,000 ceiling,
    carrying the sentinel so a leak from this field is catchable even when it is not the field under
    test."""
    return f"{_SENTINEL} " + "x" * filler


def _letter_text(filler: int = 300) -> str:
    """Valid `CoverLetter` text: well past the 200-character floor, nowhere near the 8,000 ceiling."""
    return f"{_SENTINEL} " + "y" * filler


def _short_text() -> str:
    """Non-empty, but far below either document's floor (400/200 non-whitespace characters)."""
    return f"{_SENTINEL} short"


def _too_long_cv_text() -> str:
    """Past the CV's 20,000-character ceiling; nowhere near its 400-character floor, so only the
    ceiling rule fires."""
    return f"{_SENTINEL} " + "x" * 20_500


def _too_long_letter_text() -> str:
    """Past the letter's 8,000-character ceiling, for the same reason."""
    return f"{_SENTINEL} " + "y" * 8_500


def _text_with_a_nul(filler: int, fill_char: str) -> str:
    """Valid in every respect (length, non-blank) except for one disallowed control character — a
    NUL — so only the control-character rule fires, not the floor or the ceiling."""
    return f"{_SENTINEL} " + (fill_char * filler) + "\x00"


# --- The table: every failing row raises LlmOutputInvalid with the right label ----------------------
#
# Each row is (raw, expected_problem, id). The order of the checks a compliant implementation makes
# — fence/prose stripping, then not_json, then not_object, then missing_field, then wrong_type, then
# the value objects' own rules — is fixed by parsing.py's module docstring; every fixture below is
# built to trip exactly one rule, so the row is unambiguous regardless of which check runs first.

_FAILING_ROWS: list[tuple[str, str]] = [
    (
        f"Not JSON at all — {_SENTINEL} — there isn't even a brace in here.",
        "not_json",
    ),
    (
        # Deliberately truncated: an unterminated string and a missing closing brace. Still not
        # valid JSON after fence/prose stripping, because there is neither a fence nor prose here —
        # just a broken object.
        '{"tailored_cv": "'
        + _SENTINEL
        + ' unterminated, "cover_letter": "'
        + _SENTINEL
        + " broken",
        "not_json",
    ),
    (
        json.dumps([_SENTINEL, "not an object"]),
        "not_object",
    ),
    (
        json.dumps(f"{_SENTINEL} just a bare string"),
        "not_object",
    ),
    (
        # No sentinel is embeddable here — a bare JSON number cannot carry one without ceasing to be
        # a bare top-level number (that would just be a different `not_json` row). The generic
        # no-leak assertion below still runs against this row; it is vacuously true here, and every
        # other row in this table carries the actual weight of that assertion.
        json.dumps(42),
        "not_object",
    ),
    (
        json.dumps({"tailored_cv": _cv_text()}),  # cover_letter absent entirely
        "missing_field",
    ),
    (
        json.dumps({"cover_letter": _letter_text()}),  # tailored_cv absent entirely
        "missing_field",
    ),
    (
        json.dumps({"tailored_cv": [_SENTINEL, "x"], "cover_letter": _letter_text()}),
        "wrong_type",
    ),
    (
        json.dumps({"tailored_cv": _cv_text(), "cover_letter": None}),  # JSON null, not missing
        "wrong_type",
    ),
    (
        json.dumps({"tailored_cv": _cv_text(), "cover_letter": ""}),  # blank, not below-floor
        "document_too_short",
    ),
    (
        json.dumps({"tailored_cv": _short_text(), "cover_letter": _letter_text()}),
        "document_too_short",
    ),
    (
        json.dumps({"tailored_cv": _too_long_cv_text(), "cover_letter": _letter_text()}),
        "document_too_long",
    ),
    (
        json.dumps({"tailored_cv": _cv_text(), "cover_letter": _too_long_letter_text()}),
        "document_too_long",
    ),
    (
        json.dumps({"tailored_cv": _text_with_a_nul(500, "x"), "cover_letter": _letter_text()}),
        "document_invalid",
    ),
    (
        json.dumps({"tailored_cv": _cv_text(), "cover_letter": _text_with_a_nul(300, "y")}),
        "document_invalid",
    ),
]

_FAILING_ROW_IDS = [
    "not_json_at_all",
    "not_json_truncated_object",
    "not_object_list",
    "not_object_bare_string",
    "not_object_bare_number",
    "missing_field_cover_letter",
    "missing_field_tailored_cv",
    "wrong_type_list_for_tailored_cv",
    "wrong_type_null_for_cover_letter",
    "document_too_short_blank_cover_letter",
    "document_too_short_below_floor_tailored_cv",
    "document_too_long_tailored_cv",
    "document_too_long_cover_letter",
    "document_invalid_nul_in_tailored_cv",
    "document_invalid_nul_in_cover_letter",
]


@pytest.mark.parametrize(
    ("raw", "expected_problem"),
    _FAILING_ROWS,
    ids=_FAILING_ROW_IDS,
)
def test_failing_rows_raise_llm_output_invalid_with_the_right_problem_label(
    raw: str, expected_problem: str
) -> None:
    with pytest.raises(LlmOutputInvalid) as exc_info:
        parse_tailoring_response(raw)

    assert exc_info.value.problem == expected_problem


@pytest.mark.parametrize(
    ("raw", "expected_problem"),
    _FAILING_ROWS,
    ids=_FAILING_ROW_IDS,
)
def test_failing_rows_never_leak_the_raw_response_into_the_exception(
    raw: str, expected_problem: str
) -> None:
    """AC-7/G-20: the offending text is half a CV, and this function's output travels into log
    lines and Sentry events. A general assertion across every row, not a single extra test bolted
    on the side — that is what catches a future label that helpfully interpolates the value it
    rejected, wherever in the table it lands."""
    with pytest.raises(LlmOutputInvalid) as exc_info:
        parse_tailoring_response(raw)

    assert _SENTINEL not in str(exc_info.value)
    assert _SENTINEL not in repr(exc_info.value)
    # Belt and braces: the label is the only thing this exception may carry.
    assert exc_info.value.problem == expected_problem


# --- The two rows that must NOT raise: prose and a fenced code block around otherwise-good JSON -----


def test_json_wrapped_in_surrounding_prose_still_parses() -> None:
    """A model asked for JSON still sometimes says "Sure! Here's the JSON:" — specified behaviour,
    not a failure (parsing.py's docstring, step 1)."""
    cv_text = _cv_text()
    letter_text = _letter_text()
    raw = (
        "Sure! Here's the JSON:\n"
        + json.dumps({"tailored_cv": cv_text, "cover_letter": letter_text})
        + "\nLet me know if you would like any changes!"
    )

    result = parse_tailoring_response(raw)

    assert result == TailoredDocuments(
        cv=TailoredCv(cv_text), cover_letter=CoverLetter(letter_text)
    )


def test_json_inside_a_fenced_code_block_still_parses() -> None:
    cv_text = _cv_text()
    letter_text = _letter_text()
    raw = "```json\n" + json.dumps({"tailored_cv": cv_text, "cover_letter": letter_text}) + "\n```"

    result = parse_tailoring_response(raw)

    assert result == TailoredDocuments(
        cv=TailoredCv(cv_text), cover_letter=CoverLetter(letter_text)
    )


# --- The one valid, unwrapped response ---------------------------------------------------------------


def test_valid_response_returns_both_documents_intact() -> None:
    """Both documents are present and their text equals what was sent, after the value objects' own
    normalization — constructing the expected `TailoredCv`/`CoverLetter` independently from the same
    source strings applies that normalization identically on both sides of the comparison."""
    cv_text = _cv_text()
    letter_text = _letter_text()
    raw = json.dumps({"tailored_cv": cv_text, "cover_letter": letter_text})

    result = parse_tailoring_response(raw)

    assert result == TailoredDocuments(
        cv=TailoredCv(cv_text), cover_letter=CoverLetter(letter_text)
    )
    assert result.cv.value == TailoredCv(cv_text).value
    assert result.cover_letter.value == CoverLetter(letter_text).value
