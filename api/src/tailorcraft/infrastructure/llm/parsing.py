"""`parse_tailoring_response` — the re-validation gap ADR-0004 names, closed.

*"We asked the model for JSON"* and *"this is valid JSON with the fields we need, of the types we
need, within the bounds we need"* are different claims, and the gap between them is where the 2 a.m.
bug lives. Requesting a response schema from the SDK (`response_mime_type="application/json"` plus a
two-field schema, step 4 of the technical plan's Gemini algorithm) is a **hint** to a probabilistic
system, not a guarantee from a compiler — so this function runs on **every** response regardless of
what the request asked for, and it is the only door through which a provider's bytes become
`TailoredDocuments`.

**Pure.** Its only imports are the standard library and `tailorcraft.domain.tailoring` — no SDK, no
HTTP, no clock, no settings, no database. That purity is the entire reason it can be table-tested
(AC-7, T23) without a network, an SDK, a clock or a database: the highest-value test in the slice is
a list of malformed strings and the label each one must produce, and it runs in microseconds. Keep it
that way. The moment this module needs a `Settings` or a token count it has stopped being the thing
the test can exhaustively cover.

**Written red-first even though this is infrastructure** — a deliberate exception to the tier table
in docs/sdlc.md §2, for exactly the reason `infrastructure/posting/address_policy.py` was one in
slice 1.2: the test-after tiers are the ones whose *shape is discovered against a library*, and there
is no library here to discover anything against. The contract below is fixed by the specification in
advance, like the HTTP contract, so it was written down as tests first.

**`problem` is a fixed label, never the offending text.** This is the single most important rule in
the module, and it is why the labels are constants here rather than string literals scattered through
the implementation. The offending text is half a CV (Constitution §8, failure row G-20): it would
travel into the exception's message, from there into the `llm.call_failed` log line, into a Sentry
event — `sentry_sdk` defaults `include_local_variables=True`, which neither `send_default_pii=False`
nor `max_request_body_size="never"` affects — and potentially into an API response body. A label says
*how* the answer was malformed, which is everything an operator needs and nothing a stranger's
address belongs in. No raw fragment, no excerpt, no "…first 50 characters", no character offset
quoted back with its surroundings.

The order of the checks, so that the test and the implementation agree on one reading:

1. Strip a leading/trailing markdown code fence (```` ```json ```` … ```` ``` ````) and any prose
   around the object — a model asked for JSON still sometimes says "Sure! Here's the JSON:".
2. `json.loads` → anything it refuses (including a truncated object) → `PROBLEM_NOT_JSON`.
3. The top level is not a JSON object → `PROBLEM_NOT_OBJECT`. A list, a bare string and a number all
   land here rather than in `PROBLEM_MISSING_FIELD`, because "this is not the shape at all" and "this
   is the shape with a hole in it" are different diagnoses.
4. Either of the two keys — `tailored_cv`, `cover_letter` — is absent → `PROBLEM_MISSING_FIELD`.
5. Either value is not a `str` → `PROBLEM_WRONG_TYPE`. `None` counts: a JSON `null` is a value of the
   wrong type, not an absent key.
6. Construct `TailoredCv` and `CoverLetter`, translating the domain errors they raise into
   `PROBLEM_DOCUMENT_TOO_SHORT` / `PROBLEM_DOCUMENT_TOO_LONG` / `PROBLEM_DOCUMENT_INVALID`.

**The bounds live in the value objects, not here.** `TailoredCv` and `CoverLetter` own their floors
and ceilings, their normalization and their control-character rule; this function *translates* their
errors and does not re-implement their rules. A second copy of "400 characters" in this module would
be the number that drifts — right on the day someone changes the floor in the domain, reruns the
domain tests, and never learns that the parser still enforces the old one.

Two decisions the value objects force, resolved here once so T23 and T24 do not each guess:

- `EmptyTailoredDocument` (a blank or whitespace-only document) maps to
  `PROBLEM_DOCUMENT_TOO_SHORT`. It is a distinct domain error because the value object checks blank
  before it counts, but the closed label vocabulary below has no `document_empty`, and blank is the
  limiting case of below-the-floor. Inventing an eighth label here would put a value in the code that
  the spec's failure contract does not carry.
- The translation catches `DomainError`, with the three specific errors mapped on top and
  `PROBLEM_DOCUMENT_INVALID` as the floor beneath them — the same shape, and the same reason, as the
  `except Exception` floor in the Gemini adapter. An allow-list of the domain errors we happened to
  think of is a bet that the value objects will never grow a rule, and CLAUDE.md records losing that
  exact bet in the extraction sweep. `DomainError` and not `Exception`, though: anything else
  escaping a value-object constructor is a bug in *our* code, and it belongs in the adapter's floor
  as `LlmError`, not relabelled as a malformed response.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Final

from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.domain.tailoring.errors import (
    EmptyTailoredDocument,
    InvalidTailoredDocument,
    LlmOutputInvalid,
    TailoredDocumentTooLong,
    TailoredDocumentTooShort,
)
from tailorcraft.domain.tailoring.value_objects import CoverLetter, TailoredCv, TailoredDocuments

# The closed vocabulary of `LlmOutputInvalid.problem`.
#
# `LlmOutputInvalid` types the field as a plain `str` and says why: these labels are a *parser's*
# vocabulary for how a response was malformed, which is an infrastructure concern, and the domain's
# vocabulary stops at "the output was invalid". An enum in `domain/tailoring/errors.py` would drag
# this taxonomy across the boundary and would have to grow every time the parser learns to recognise
# a new malformation. So they are matched to that `str` the only way that keeps them honest: as named
# constants in one place, in the one module that raises them.
#
# They are values, not prose. Each one is read by a `structlog` field (`problem=…`) and may end up
# grouped in a dashboard, so they are stable, lowercase, snake_case, and changing one is a
# behaviour change — not a rename.
PROBLEM_NOT_JSON: Final = "not_json"
PROBLEM_NOT_OBJECT: Final = "not_object"
PROBLEM_MISSING_FIELD: Final = "missing_field"
PROBLEM_WRONG_TYPE: Final = "wrong_type"
PROBLEM_DOCUMENT_TOO_SHORT: Final = "document_too_short"
PROBLEM_DOCUMENT_TOO_LONG: Final = "document_too_long"
PROBLEM_DOCUMENT_INVALID: Final = "document_invalid"

# The two keys the response schema asks for (technical plan, step 4). Named constants because the
# same two strings appear in `prompt.py`'s output contract and in the SDK schema the adapter builds;
# three copies of a literal is how a renamed field becomes a `missing_field` nobody can explain.
KEY_TAILORED_CV: Final = "tailored_cv"
KEY_COVER_LETTER: Final = "cover_letter"

_FENCE: Final = "```"


def _inside_a_code_fence(text: str) -> str | None:
    """The body of a ```` ``` ````-fenced block, with its optional info string (```` ```json ````)
    dropped, or `None` if `text` is not one whole fenced block.

    Deliberately narrow: it recognises the shape a model actually emits — one fence opening the
    answer and one closing it — and refuses to go looking for a fence in the middle of prose. A
    cleverer scanner would be a second, worse JSON parser, and the brace slice below already covers
    "the object is somewhere in there".
    """
    if not (text.startswith(_FENCE) and text.endswith(_FENCE) and len(text) > 2 * len(_FENCE)):
        return None
    body = text[len(_FENCE) : -len(_FENCE)]
    # ```` ```json ```` — the info string is everything up to the first newline. It is dropped only
    # when it looks like a language tag (alphanumeric), so a fence whose first line is already the
    # payload survives intact.
    info, newline, remainder = body.partition("\n")
    if newline and info.strip().isalnum():
        body = remainder
    return body.strip()


def _between_the_outermost_braces(text: str) -> str | None:
    """The slice from the first `{` to the last `}`, or `None` if there is no such pair.

    This is the "any prose around the object" half of step 1 — *"Sure! Here's the JSON:"* before it
    and *"Let me know if you'd like changes!"* after it. It is tried **last**, after the whole
    response has already failed to parse on its own, because it is lossy by construction: a response
    that is legitimately a bare JSON string, list or number has no braces to slice between and must
    reach `PROBLEM_NOT_OBJECT` rather than being mangled into `PROBLEM_NOT_JSON`. A truncated object
    — an opening brace and no closing one — also lands here with nothing to return, which is why
    that row is `not_json` and not something subtler.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    return text[start : end + 1]


def _json_candidates(raw: str) -> Iterator[str]:
    """The strings that might be the JSON, most-faithful first.

    Order is the whole design. The untouched response is tried before any unwrapping, so that a
    well-formed answer is never reinterpreted by a heuristic that only exists for malformed ones.
    """
    stripped = raw.strip()
    yield stripped

    fenced = _inside_a_code_fence(stripped)
    if fenced is not None:
        yield fenced

    braced = _between_the_outermost_braces(stripped)
    if braced is not None:
        yield braced


def _decode(raw: str) -> object:
    """Step 1 and step 2: unwrap, then `json.loads`. Raises `LlmOutputInvalid(PROBLEM_NOT_JSON)`.

    Typed `object`, not `Any`: the caller has to narrow before it can touch anything, which is how
    steps 3-5 below stay checked rather than merely written.
    """
    for candidate in _json_candidates(raw):
        try:
            return json.loads(candidate)
        except ValueError:
            # Not `except json.JSONDecodeError`: `json.loads` also raises a bare `ValueError` for
            # some inputs (a NaN in a strict decoder, a recursion limit), and every one of them
            # means the same thing here. Nothing about the failure is captured — see below.
            continue
    # `from None`, and this is not decoration. `json.JSONDecodeError` keeps the **entire document**
    # in its `.doc` attribute, so a chained cause would carry the whole half-written CV into the
    # traceback, into Sentry (`include_local_variables=True` by default) and into any handler that
    # walks `__cause__`. The label is all an operator gets and all they need. (The loop's `continue`
    # has already cleared the implicit context; this says so out loud.)
    raise LlmOutputInvalid(PROBLEM_NOT_JSON) from None


def _problem_for(error: DomainError) -> str:
    """Step 6's translation: a value object's complaint → one of the closed labels.

    The three specific errors are mapped on top and `PROBLEM_DOCUMENT_INVALID` is the floor beneath
    them, the same shape and the same reason as the `except Exception` floor in the Gemini adapter:
    an allow-list of the domain errors we happened to think of is a bet that `TailoredCv` and
    `CoverLetter` will never grow a rule.
    """
    match error:
        # `EmptyTailoredDocument` is a distinct domain error because the value object checks blank
        # before it counts, but the label vocabulary above is closed at seven and has no
        # `document_empty`: blank is the limiting case of below-the-floor. Inventing an eighth label
        # here would put a value in the code that the spec's failure contract does not carry.
        case EmptyTailoredDocument() | TailoredDocumentTooShort():
            return PROBLEM_DOCUMENT_TOO_SHORT
        case TailoredDocumentTooLong():
            return PROBLEM_DOCUMENT_TOO_LONG
        case InvalidTailoredDocument():
            return PROBLEM_DOCUMENT_INVALID
        case _:
            # Not a duplicate of the arm above it, and do not "simplify" the two into one. The arm
            # above is a *mapping* — `InvalidTailoredDocument` reaches this label by name. This one
            # is the *floor* — an unrecognised `DomainError` reaches it by exhaustion, because a
            # malformed document is the least wrong thing to call a rule we have not met yet. They
            # coincide in value today and mean different things; keeping them apart is what lets a
            # future rule be mapped on top without first untangling one arm into two.
            return PROBLEM_DOCUMENT_INVALID


def parse_tailoring_response(raw: str) -> TailoredDocuments:
    """Turn one raw provider completion into the pair of validated documents, or refuse.

    Args:
        raw: the model's response text, exactly as the SDK handed it over — possibly fenced, possibly
            wrapped in prose, possibly truncated, possibly not JSON at all. **Never logged, never
            echoed, never put in an exception message.**

    Returns:
        `TailoredDocuments` — both documents present and each one through its value object's rules.
        There is no partial success: TR-5 makes "succeeded with no cover letter" unrepresentable.

    Raises:
        LlmOutputInvalid: with one of the `PROBLEM_*` labels above, in the order this module's
            docstring fixes. The label is the *whole* payload — no fragment of `raw` travels with it.
    """
    # Steps 1 and 2: unwrap a fence or surrounding prose, then decode. → PROBLEM_NOT_JSON.
    payload = _decode(raw)

    # Step 3. A list, a bare string and a number all land here rather than in PROBLEM_MISSING_FIELD,
    # because "this is not the shape at all" and "this is the shape with a hole in it" are different
    # diagnoses for whoever reads the label off a dashboard.
    if not isinstance(payload, dict):
        raise LlmOutputInvalid(PROBLEM_NOT_OBJECT)

    # Step 4. Membership, not `.get(...)`, and that is the whole difference between this check and
    # the next one: a key present with a `null` value is `wrong_type`, not `missing_field`.
    if KEY_TAILORED_CV not in payload or KEY_COVER_LETTER not in payload:
        raise LlmOutputInvalid(PROBLEM_MISSING_FIELD)

    cv_value = payload[KEY_TAILORED_CV]
    cover_letter_value = payload[KEY_COVER_LETTER]

    # Step 5. Both narrowed in one condition so that `mypy` carries `str` into the constructors
    # below — a per-key loop reads better and narrows nothing.
    if not isinstance(cv_value, str) or not isinstance(cover_letter_value, str):
        raise LlmOutputInvalid(PROBLEM_WRONG_TYPE)

    # Step 6. One `try` around both constructions rather than two: the first document to complain
    # decides the label, and there is no row in the failure contract that wants to know about the
    # second problem once the response is already being refused.
    #
    # `DomainError` and not `Exception`: anything else escaping a value-object constructor is a bug
    # in *our* code, and it belongs in the adapter's floor as `LlmError` rather than being relabelled
    # as a malformed response — which would quietly blame the provider for our defect.
    try:
        cv = TailoredCv(cv_value)
        cover_letter = CoverLetter(cover_letter_value)
    except DomainError as error:
        # `from None` for the same reason as in `_decode`, one notch sharper: the value object's own
        # message carries a character count, and its frame carries the normalized document. Neither
        # belongs in a traceback that a log handler or Sentry will walk.
        raise LlmOutputInvalid(_problem_for(error)) from None

    return TailoredDocuments(cv=cv, cover_letter=cover_letter)
