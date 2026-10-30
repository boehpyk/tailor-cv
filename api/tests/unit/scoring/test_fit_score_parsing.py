"""`parse_fit_score_response` -- the re-validation table (T11; feature-spec S-4, S-5, AC-24 to AC-27).

Pure: raw strings in, a `FitAssessment` or a `FitScoreResponseInvalid` out. No SDK, clock, database or
mocks. Written from the spec and the module docstring's check order, not from the code (which is a
`NotImplementedError` skeleton), and from the technical plan's "Corrections made during
implementation" (T10): a blank quote, or one under 8 characters once whitespace is normalized, is
`gap_not_in_posting`; duplicate gaps are `gaps_inconsistent`; `too_many_gaps` is judged before any
single gap; within a gap `gap_invalid` is judged before the quote.

The label strings are hard-coded, not imported: the vocabulary is fixed by the spec's S-4 and S-5
rows, and a test that imports the string it checks cannot disagree with the code.

Every refusal fixture carries a sentinel in the fields the model controls, and the leak test asserts
that no label quotes it (a CV-derived value reaches logs and Sentry through an exception message).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tailorcraft.domain.scoring.value_objects import FitAssessment
from tailorcraft.infrastructure.llm.fit_score_parsing import (
    FitScoreResponseInvalid,
    parse_fit_score_response,
)

POSTING = "We need strong Kubernetes experience and AWS certification.\n  Remote   only."
SENTINEL = "ZXQ-SENTINEL-9f3a"

REQ_A = "Kubernetes experience"
QUOTE_A = "strong Kubernetes experience"
REQ_B = "AWS certification"
QUOTE_B = "AWS certification"
REQ_C = "Remote working"
QUOTE_C = "Remote only"


def gap(requirement: str = REQ_A, quote: str = QUOTE_A) -> dict[str, Any]:
    return {"requirement": requirement, "posting_quote": quote}


def doc(score: Any = 60, gaps: Any = None) -> str:
    return json.dumps({"score": score, "gaps": [gap()] if gaps is None else gaps})


def refused(raw: str, problem: str, posting: str = POSTING) -> FitScoreResponseInvalid:
    with pytest.raises(FitScoreResponseInvalid) as info:
        parse_fit_score_response(raw, posting)
    assert info.value.problem == problem
    return info.value


# --- Accepted ------------------------------------------------------------------------------------


def test_a_well_formed_response_becomes_an_assessment() -> None:
    result = parse_fit_score_response(doc(60, [gap(), gap(REQ_B, QUOTE_B)]), POSTING)
    assert isinstance(result, FitAssessment)
    assert result.score.value == 60
    assert [g.value for g in result.gaps] == [REQ_A, REQ_B]


def test_a_json_code_fence_around_good_json_is_stripped() -> None:
    result = parse_fit_score_response(f"```json\n{doc()}\n```", POSTING)
    assert result.score.value == 60


def test_a_bare_code_fence_around_good_json_is_stripped() -> None:
    assert parse_fit_score_response(f"```\n{doc()}\n```", POSTING).score.value == 60


def test_prose_around_the_object_is_stripped() -> None:
    raw = f"Here is my assessment:\n{doc()}\nHope that helps!"
    assert parse_fit_score_response(raw, POSTING).score.value == 60


def test_three_gaps_are_accepted() -> None:
    gaps = [gap(), gap(REQ_B, QUOTE_B), gap(REQ_C, QUOTE_C)]
    assert len(parse_fit_score_response(doc(40, gaps), POSTING).gaps) == 3


@pytest.mark.parametrize("score", [0, 100])
def test_the_score_bounds_are_accepted(score: int) -> None:
    assert parse_fit_score_response(doc(score), POSTING).score.value == score


@pytest.mark.parametrize("score", [90, 100])
def test_no_gaps_is_accepted_at_90_and_above(score: int) -> None:
    result = parse_fit_score_response(doc(score, []), POSTING)
    assert result.gaps == ()


def test_a_requirement_is_returned_with_whitespace_collapsed() -> None:
    result = parse_fit_score_response(doc(60, [gap("  AWS   certification ")]), POSTING)
    assert result.gaps[0].value == "AWS certification"


def test_a_quote_differing_only_in_case_is_accepted() -> None:
    raw = doc(60, [gap(quote="STRONG kubernetes EXPERIENCE")])
    assert len(parse_fit_score_response(raw, POSTING).gaps) == 1


def test_a_quote_differing_only_in_whitespace_is_accepted() -> None:
    raw = doc(60, [gap(quote="  strong\tKubernetes\n experience ")])
    assert len(parse_fit_score_response(raw, POSTING).gaps) == 1


def test_a_quote_spanning_the_postings_own_line_break_is_accepted() -> None:
    raw = doc(60, [gap(REQ_C, "certification. Remote only")])
    assert len(parse_fit_score_response(raw, POSTING).gaps) == 1


def test_a_quote_of_exactly_eight_characters_is_accepted() -> None:
    assert len("AWS cert") == 8
    assert len(parse_fit_score_response(doc(60, [gap(REQ_B, "AWS cert")]), POSTING).gaps) == 1


# --- S-4: not the shape at all ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "I cannot help with that.", '{"score": 60, "gaps": [', "{'score': 60, 'gaps': []}"],
)
def test_text_that_is_not_json_is_not_json(raw: str) -> None:
    refused(raw, "not_json")


@pytest.mark.parametrize("raw", ["[]", "42", "null", '"text"', "true"])
def test_json_that_is_not_an_object_is_not_object(raw: str) -> None:
    refused(raw, "not_object")


@pytest.mark.parametrize(
    "raw",
    ["{}", '{"score": 50}', '{"gaps": []}', '{"Score": 50, "gaps": []}'],
)
def test_an_absent_top_level_key_is_missing_field(raw: str) -> None:
    refused(raw, "missing_field")


@pytest.mark.parametrize("score", [True, False, 50.5, 50.0, "50", None, [50], {"v": 50}])
def test_a_score_that_is_not_an_int_is_wrong_type(score: Any) -> None:
    refused(doc(score), "wrong_type")


@pytest.mark.parametrize("gaps", ["none", None, {}, {"requirement": REQ_A}, 3])
def test_gaps_that_are_not_a_list_are_wrong_type(gaps: Any) -> None:
    refused(json.dumps({"score": 60, "gaps": gaps}), "wrong_type")


@pytest.mark.parametrize("item", ["Kubernetes experience", None, 3, ["a"]])
def test_a_gap_that_is_not_an_object_is_wrong_type(item: Any) -> None:
    refused(doc(60, [item]), "wrong_type")


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"requirement": REQ_A},
        {"posting_quote": QUOTE_A},
        {"requirement": REQ_A, "quote": QUOTE_A},
    ],
)
def test_a_key_absent_from_a_gap_is_missing_field(item: dict[str, Any]) -> None:
    refused(doc(60, [item]), "missing_field")


@pytest.mark.parametrize(
    "item",
    [
        {"requirement": 5, "posting_quote": QUOTE_A},
        {"requirement": None, "posting_quote": QUOTE_A},
        {"requirement": REQ_A, "posting_quote": None},
        {"requirement": REQ_A, "posting_quote": ["x"]},
    ],
)
def test_a_gap_value_that_is_not_a_string_is_wrong_type(item: dict[str, Any]) -> None:
    refused(doc(60, [item]), "wrong_type")


# --- S-5: it parses, and it is wrong -----------------------------------------------------------


@pytest.mark.parametrize("score", [-1, 101, 1000])
def test_a_score_outside_0_to_100_is_out_of_range(score: int) -> None:
    refused(doc(score), "score_out_of_range")


def test_four_gaps_are_too_many_and_never_truncated() -> None:
    gaps = [
        gap(),
        gap(REQ_B, QUOTE_B),
        gap(REQ_C, QUOTE_C),
        gap("Another requirement", "Remote   only"),
    ]
    refused(doc(40, gaps), "too_many_gaps")


@pytest.mark.parametrize(
    "requirement",
    [
        "",
        "ab",
        "x" * 121,
        "See https://example.com/job",
        "Contact hr@example.com",
        "Phone 1234567",
        "Needs <b>Kubernetes</b>",
        "Line one\nline two",
        "Tab\x00control",
    ],
)
def test_a_requirement_the_gap_refuses_is_gap_invalid(requirement: str) -> None:
    refused(doc(60, [gap(requirement)]), "gap_invalid")


@pytest.mark.parametrize(
    "quote",
    [
        "Terraform and Go",  # absent from the posting
        "",  # the empty string is a substring of every posting
        "   \t\n ",
        "AWS cer",  # 7 characters
        "  Kuberne  ",  # 7 after normalization, however padded
        "K",
    ],
)
def test_a_quote_that_is_blank_short_or_absent_is_not_in_posting(quote: str) -> None:
    refused(doc(60, [gap(quote=quote)]), "gap_not_in_posting")


def test_a_quote_not_in_the_posting_is_refused_whatever_the_requirement_says() -> None:
    refused(doc(60, [gap(REQ_B, "Kubernetes and Terraform")]), "gap_not_in_posting")


def test_no_gaps_below_90_is_inconsistent() -> None:
    refused(doc(89, []), "gaps_inconsistent")


def test_a_zero_score_with_no_gaps_is_inconsistent() -> None:
    refused(doc(0, []), "gaps_inconsistent")


def test_a_repeated_gap_is_inconsistent() -> None:
    refused(doc(60, [gap(), gap()]), "gaps_inconsistent")


def test_a_repeated_gap_differing_only_in_case_is_inconsistent() -> None:
    refused(doc(60, [gap(), gap("KUBERNETES EXPERIENCE", QUOTE_A)]), "gaps_inconsistent")


# --- The check order, wherever an input breaks two rules ----------------------------------------


def test_missing_field_is_judged_before_wrong_type() -> None:
    refused('{"score": "50"}', "missing_field")


def test_wrong_type_is_judged_before_range() -> None:
    refused(doc(50.5 + 100, []), "wrong_type")


def test_score_range_is_judged_before_too_many_gaps() -> None:
    four = [gap(REQ_A + str(i)) for i in range(4)]
    refused(doc(101, four), "score_out_of_range")


def test_too_many_gaps_is_judged_before_an_invalid_gap() -> None:
    four = [gap("ab")] + [gap(REQ_B, QUOTE_B)] * 3
    refused(doc(40, four), "too_many_gaps")


def test_too_many_gaps_is_judged_before_duplicates() -> None:
    refused(doc(40, [gap()] * 4), "too_many_gaps")


def test_gap_invalid_is_judged_before_the_quote_within_one_gap() -> None:
    refused(doc(60, [gap("See https://example.com", "Terraform and Go")]), "gap_invalid")


def test_a_gap_is_judged_in_order_so_the_first_bad_gap_names_the_problem() -> None:
    refused(doc(60, [gap("ab"), gap(quote="Terraform and Go")]), "gap_invalid")
    refused(doc(60, [gap(quote="Terraform and Go"), gap("ab")]), "gap_not_in_posting")


def test_a_per_gap_refusal_is_judged_before_the_cross_gap_checks() -> None:
    refused(doc(60, [gap(), gap(quote="Terraform and Go")]), "gap_not_in_posting")


def test_a_wrong_typed_gap_is_judged_before_a_later_invalid_one() -> None:
    refused(doc(60, [{"requirement": 5, "posting_quote": QUOTE_A}, gap("ab")]), "wrong_type")


# --- A label never quotes the input --------------------------------------------------------------

_LEAK_ROWS: list[str] = [
    f"I cannot help. {SENTINEL}",
    f'{{"score": 60, "gaps": [{{"requirement": "{SENTINEL}"',
    f'["{SENTINEL}"]',
    json.dumps({"score": SENTINEL, "gaps": [gap(SENTINEL, SENTINEL)]}),
    json.dumps({"score": 60, "gaps": SENTINEL}),
    json.dumps({"score": 60, "gaps": [{"requirement": SENTINEL}]}),
    json.dumps({"score": 60, "gaps": [{"requirement": 5, "posting_quote": SENTINEL}]}),
    json.dumps({"score": 101, "gaps": [gap(SENTINEL, SENTINEL)]}),
    doc(40, [gap(f"{SENTINEL} one", QUOTE_A)] * 4),
    doc(60, [gap(f"{SENTINEL} https://example.com")]),
    doc(60, [gap(REQ_A, f"{SENTINEL} not in the posting")]),
    doc(60, [gap(f"{SENTINEL} item", QUOTE_A)] * 2),
]


@pytest.mark.parametrize("raw", _LEAK_ROWS)
def test_no_label_quotes_the_response_or_the_posting(raw: str) -> None:
    posting = f"{POSTING} {SENTINEL}-posting-only"
    with pytest.raises(FitScoreResponseInvalid) as info:
        parse_fit_score_response(raw, posting)
    text = f"{info.value!s} {info.value!r} {info.value.args!r}"
    assert SENTINEL not in text
    assert "posting-only" not in text


@pytest.mark.parametrize("raw", _LEAK_ROWS)
def test_a_refusal_hides_its_cause(raw: str) -> None:
    """`from None`: a `JSONDecodeError` keeps the whole document in `.doc`."""
    with pytest.raises(FitScoreResponseInvalid) as info:
        parse_fit_score_response(raw, POSTING)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True
