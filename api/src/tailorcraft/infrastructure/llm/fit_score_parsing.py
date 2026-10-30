"""`parse_fit_score_response`: the only door through which a provider's bytes become a `FitAssessment`.

The response schema is a hint and this is the guarantee, so it runs on every response. Pure, for
`parsing.py`'s reason: its only imports are the standard library and `domain.scoring`, which is what
lets a table of malformed strings cover it in microseconds. Written red-first, for the same reason.

**A label is the whole payload.** It says how an answer was malformed and never quotes it: the text
is derived from a CV, and an exception's message reaches logs and Sentry. Each refusal raises `from
None`, because a `JSONDecodeError` keeps the entire document in `.doc`.

The order of the checks, so the test and the implementation agree on one reading:

1. Strip a code fence or the prose around the object, then `json.loads`, else `PROBLEM_NOT_JSON`.
2. The top level is not an object: `PROBLEM_NOT_OBJECT`.
3. `score` or `gaps` is absent: `PROBLEM_MISSING_FIELD`.
4. `score` is not an `int` (a bool, a float, a string, null), or `gaps` is not a list:
   `PROBLEM_WRONG_TYPE`.
5. `score` is outside 0 to 100: `PROBLEM_SCORE_OUT_OF_RANGE`.
6. More than three gaps: `PROBLEM_TOO_MANY_GAPS`. Never truncated.
7. For each gap in order: not an object is `PROBLEM_WRONG_TYPE`; a key absent is
   `PROBLEM_MISSING_FIELD`; a value not a `str` is `PROBLEM_WRONG_TYPE`; `requirement` refused by
   `FitGap` is `PROBLEM_GAP_INVALID`; `posting_quote` under `MIN_POSTING_QUOTE_LENGTH` characters
   once whitespace is collapsed, or not a normalized (case-folded, whitespace collapsed) substring
   of the posting, is `PROBLEM_GAP_NOT_IN_POSTING`. The floor is what makes the check mean anything:
   the empty string is a substring of every posting, and one letter of nearly every posting.
8. No gaps below a score of 90, or a repeated gap: `PROBLEM_GAPS_INCONSISTENT`.

The quote stops here: it is not on the `FitAssessment` this returns (AC-26).
"""

from __future__ import annotations

import json
import re
from typing import Final

from tailorcraft.domain.scoring.errors import (
    InvalidFitAssessment,
    InvalidFitGap,
    InvalidScorePercent,
)
from tailorcraft.domain.scoring.value_objects import (
    _MAX_GAPS,
    FitAssessment,
    FitGap,
    ScorePercent,
)

# S-4: the answer is not the shape at all, or the shape with a hole in it.
PROBLEM_NOT_JSON: Final = "not_json"
PROBLEM_NOT_OBJECT: Final = "not_object"
PROBLEM_MISSING_FIELD: Final = "missing_field"
PROBLEM_WRONG_TYPE: Final = "wrong_type"
# S-5: it parses, and it is wrong.
PROBLEM_SCORE_OUT_OF_RANGE: Final = "score_out_of_range"
PROBLEM_TOO_MANY_GAPS: Final = "too_many_gaps"
PROBLEM_GAP_INVALID: Final = "gap_invalid"
PROBLEM_GAP_NOT_IN_POSTING: Final = "gap_not_in_posting"
PROBLEM_GAPS_INCONSISTENT: Final = "gaps_inconsistent"

# The keys the schema and the prompt share (plan §0.6), so a renamed one cannot drift.
KEY_SCORE: Final = "score"
KEY_GAPS: Final = "gaps"
KEY_REQUIREMENT: Final = "requirement"
KEY_POSTING_QUOTE: Final = "posting_quote"

# Shared with the prompt, which has to tell the model about it (plan, corrections, T10).
MIN_POSTING_QUOTE_LENGTH: Final = 8

_FENCE: Final = re.compile(r"```\w*\s(.*)```", re.DOTALL)


class FitScoreResponseInvalid(Exception):
    """The answer was refused. Carries a `PROBLEM_*` label and nothing else.

    Not `FitScoringOutputInvalid`: that one needs the call's accumulated usage, which a pure parser
    does not have. The adapter translates this one, adding the usage (T26).
    """

    def __init__(self, problem: str) -> None:
        super().__init__(problem)
        self.problem = problem


def _decode(raw: str) -> object:
    """Step 1: the untouched text first, then inside a fence, then between the outermost braces.

    The slice is empty or starts with a stray `}` when there is no such pair, and neither parses.
    `RecursionError` because deep nesting raises that, and it is not a `ValueError`.
    """
    stripped = raw.strip()
    candidates = [stripped]
    if fence := _FENCE.fullmatch(stripped):
        candidates.append(fence[1])
    candidates.append(stripped[stripped.find("{") : stripped.rfind("}") + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (ValueError, RecursionError):
            continue
    raise FitScoreResponseInvalid(PROBLEM_NOT_JSON) from None


def _collapsed(text: str) -> str:
    return " ".join(text.split())


def _parsed_gap(item: object, posting: str) -> FitGap:
    """Step 7, for one gap. `posting` is already normalized. The quote is judged and dropped here."""
    if not isinstance(item, dict):
        raise FitScoreResponseInvalid(PROBLEM_WRONG_TYPE) from None
    if KEY_REQUIREMENT not in item or KEY_POSTING_QUOTE not in item:
        raise FitScoreResponseInvalid(PROBLEM_MISSING_FIELD) from None
    requirement, quote = item[KEY_REQUIREMENT], item[KEY_POSTING_QUOTE]
    if not isinstance(requirement, str) or not isinstance(quote, str):
        raise FitScoreResponseInvalid(PROBLEM_WRONG_TYPE) from None
    try:
        gap = FitGap(requirement)
    except InvalidFitGap:
        raise FitScoreResponseInvalid(PROBLEM_GAP_INVALID) from None
    # Measured before case-folding, which can change a string's length.
    collapsed = _collapsed(quote)
    if len(collapsed) < MIN_POSTING_QUOTE_LENGTH or collapsed.casefold() not in posting:
        raise FitScoreResponseInvalid(PROBLEM_GAP_NOT_IN_POSTING) from None
    return gap


def parse_fit_score_response(raw: str, posting_text: str) -> FitAssessment:
    """Turn one raw completion into a validated assessment, or raise `FitScoreResponseInvalid`.

    Args:
        raw: the model's text, exactly as the SDK handed it over. Never logged or echoed.
        posting_text: the posting the gaps must quote. Never logged or echoed.
    """
    payload = _decode(raw)
    if not isinstance(payload, dict):
        raise FitScoreResponseInvalid(PROBLEM_NOT_OBJECT) from None
    if KEY_SCORE not in payload or KEY_GAPS not in payload:
        raise FitScoreResponseInvalid(PROBLEM_MISSING_FIELD) from None
    score, gaps = payload[KEY_SCORE], payload[KEY_GAPS]
    # `type`, not `isinstance`: a `bool` is an `int` to Python.
    if type(score) is not int or not isinstance(gaps, list):
        raise FitScoreResponseInvalid(PROBLEM_WRONG_TYPE) from None
    try:
        percent = ScorePercent(score)
    except InvalidScorePercent:
        raise FitScoreResponseInvalid(PROBLEM_SCORE_OUT_OF_RANGE) from None
    if len(gaps) > _MAX_GAPS:
        raise FitScoreResponseInvalid(PROBLEM_TOO_MANY_GAPS) from None
    posting = _collapsed(posting_text).casefold()
    fit_gaps = tuple(_parsed_gap(item, posting) for item in gaps)
    try:
        return FitAssessment(percent, fit_gaps)
    except InvalidFitAssessment:
        raise FitScoreResponseInvalid(PROBLEM_GAPS_INCONSISTENT) from None
