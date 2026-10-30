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
   `FitGap` is `PROBLEM_GAP_INVALID`; `posting_quote` blank or not a normalized (case-folded,
   whitespace collapsed) substring of the posting is `PROBLEM_GAP_NOT_IN_POSTING`. Blank is named
   because the empty string is a substring of every posting.
8. No gaps below a score of 90, or a repeated gap: `PROBLEM_GAPS_INCONSISTENT`.

The quote stops here: it is not on the `FitAssessment` this returns (AC-26).
"""

from __future__ import annotations

from typing import Final

from tailorcraft.domain.scoring.value_objects import FitAssessment

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


class FitScoreResponseInvalid(Exception):
    """The answer was refused. Carries a `PROBLEM_*` label and nothing else.

    Not `FitScoringOutputInvalid`: that one needs the call's accumulated usage, which a pure parser
    does not have. The adapter translates this one, adding the usage (T26).
    """

    def __init__(self, problem: str) -> None:
        super().__init__(problem)
        self.problem = problem


def parse_fit_score_response(raw: str, posting_text: str) -> FitAssessment:
    """Turn one raw completion into a validated assessment, or raise `FitScoreResponseInvalid`.

    Args:
        raw: the model's text, exactly as the SDK handed it over. Never logged or echoed.
        posting_text: the posting the gaps must quote. Never logged or echoed.
    """
    raise NotImplementedError
