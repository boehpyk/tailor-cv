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

**SKELETON (T22).** The body raises `NotImplementedError`; `qa` records the red at T23 and T24 fills
it in. Red-first even though this is infrastructure — a deliberate exception to the tier table in
docs/sdlc.md §2, for exactly the reason `infrastructure/posting/address_policy.py` was one in slice
1.2: the test-after tiers are the ones whose *shape is discovered against a library*, and there is no
library here to discover anything against. The contract below is fixed by the specification in
advance, like the HTTP contract, so it is written down as tests first.

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

from typing import Final

from tailorcraft.domain.tailoring.value_objects import TailoredDocuments

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
    raise NotImplementedError("T24 implements the re-validation; T23 records the red.")
