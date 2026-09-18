"""`parse_document`, `normalize_to_grammar` and `_allow_three_schemes` — the first lock and the
rendering clamp (AC-28, ADR-0017 obligations 1 and 2, X-51 … X-53, X-56).

Pure domain-adjacent tests: no I/O, no event loop, no fixtures, no mocks. Every assertion below
comes from the feature spec's AC-28 and the technical plan's "The render, step by step" §1-2 — never
from running the code, which at the time of writing raises `NotImplementedError` from every
behavioural function on purpose (docs/sdlc.md §2; `infrastructure/export/tokens.py`'s own
module docstring says so explicitly). A `NotImplementedError` escaping straight out of
`parse_document` or `_allow_three_schemes` is a valid red here: the call reached the right function
and it refused, which is what the RED tier promises for a skeleton.

`GRAMMAR_RULES`, by contrast, is written whole in the skeleton — a constant *is* its value, the way
a dataclass's field list is its signature — so the tests over it are **green on arrival**: nothing
was deferred, there is no body for a red to discriminate against.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from markdown_it.token import Token

from tailorcraft.infrastructure.export.plain_text import render_plain_text
from tailorcraft.infrastructure.export.tokens import (
    GRAMMAR_RULES,
    _allow_three_schemes,
    normalize_to_grammar,
    parse_document,
    strip_refused_link_markup,
)

# --- GRAMMAR_RULES: written whole in the skeleton, so this table is green on arrival ---------------
#
# Typed out from the technical plan's step 1 and ADR-0017 §2 row 1, never read back off the
# constant under test — a test that compared `GRAMMAR_RULES` to itself would ratify any future typo.


def test_grammar_rules_is_exactly_the_planned_nine_rules_in_order() -> None:
    assert GRAMMAR_RULES == (
        "paragraph",
        "heading",
        "list",
        "newline",
        "escape",
        "emphasis",
        "link",
        "text",
        "entity",
    )


def test_grammar_rules_is_a_tuple_not_a_list() -> None:
    """Order is part of the value (the RED-commit briefing calls this out explicitly), and a tuple
    is also immutable in a way a list is not — nobody can `.append()` a tenth rule onto it at
    import time."""
    assert isinstance(GRAMMAR_RULES, tuple)


def test_grammar_rules_excludes_image_html_and_linkify() -> None:
    """The absences that matter most, named individually rather than only by what the full-equality
    check above happens to leave out: `image` (X-53 — an image must be unrepresentable, not merely
    unrendered), `html_inline`/`html_block` (X-51 — belt to `html=False`'s braces), and `linkify`
    (not installed, not enabled — nothing may manufacture an `href` the author never wrote)."""
    for absent_rule in ("image", "html_inline", "html_block", "linkify"):
        assert absent_rule not in GRAMMAR_RULES


def test_grammar_rules_excludes_table_fence_code_blockquote_hr_and_strikethrough() -> None:
    """Outside the editor's grammar (ADR-0015), so these render as literal text (X-56)."""
    for absent_rule in ("table", "fence", "code", "blockquote", "hr", "strikethrough"):
        assert absent_rule not in GRAMMAR_RULES


# --- _allow_three_schemes: the table AC-28 asks for directly ---------------------------------------
#
# `http`, `https`, `mailto` accepted; `javascript:`, `data:`, `file:`, `vbscript:`, an unknown
# scheme and a protocol-relative `//host` (no scheme at all under `urlsplit`) refused.

_SCHEME_TABLE: list[tuple[str, bool]] = [
    ("http://example.com/cv.pdf", True),
    ("https://example.com/cv.pdf", True),
    ("mailto:person@example.com", True),
    ("javascript:alert(1)", False),
    ("data:text/html,%3Cscript%3Ealert(1)%3C/script%3E", False),
    ("file:///etc/passwd", False),
    ("vbscript:msgbox(1)", False),
    ("ftp://example.com/cv.pdf", False),
    ("//evil.example.com/cv.pdf", False),
]

_SCHEME_TABLE_IDS = [
    "http_accepted",
    "https_accepted",
    "mailto_accepted",
    "javascript_refused",
    "data_refused",
    "file_refused",
    "vbscript_refused",
    "unknown_scheme_refused",
    "protocol_relative_no_scheme_refused",
]


@pytest.mark.parametrize(("url", "expected"), _SCHEME_TABLE, ids=_SCHEME_TABLE_IDS)
def test_allow_three_schemes_table(url: str, expected: bool) -> None:
    assert _allow_three_schemes(url) is expected


# --- AC-28's hostile fixture -------------------------------------------------------------------------

_HOSTILE_FIXTURE = (
    "<script>alert(1)</script>\n\n"
    "<img src=x onerror=alert(1)>\n\n"
    "<iframe></iframe>\n\n"
    "<style>@import url(http://evil)</style>\n\n"
    "[x](javascript:alert(1))\n\n"
    "[x](data:text/html,%3Cb%3E)\n\n"
    "![x](http://evil/i.png)\n"
)


def _all_tokens(tokens: Sequence[Token]) -> list[Token]:
    """Flatten a token stream including inline children, so a link buried inside a paragraph's
    `inline` token is still visible to an assertion that walks "every token"."""
    flat: list[Token] = []
    for token in tokens:
        flat.append(token)
        if token.children:
            flat.extend(_all_tokens(token.children))
    return flat


def test_hostile_fixture_produces_no_html_inline_or_html_block_tokens() -> None:
    """X-51 / AC-28: `html=False` makes raw HTML a text token, never markup."""
    tokens = normalize_to_grammar(parse_document(_HOSTILE_FIXTURE))

    token_types = {token.type for token in _all_tokens(tokens)}
    assert "html_inline" not in token_types
    assert "html_block" not in token_types


def test_hostile_fixture_produces_no_image_token() -> None:
    """X-53: `image` is not an enabled rule, so an image is unrepresentable, not merely stripped."""
    tokens = normalize_to_grammar(parse_document(_HOSTILE_FIXTURE))

    token_types = {token.type for token in _all_tokens(tokens)}
    assert "image" not in token_types


def test_hostile_fixture_produces_no_link_with_an_href_outside_the_three_schemes() -> None:
    """X-52: `javascript:` and `data:` hrefs must never survive the parse. A link whose scheme is
    refused keeps its text but loses its `href` (tokens.py's own docstring: "nothing about the
    document disappears") — so this asserts every surviving `link_open` token's href is either
    empty/absent or one of the three allowed schemes, never a disallowed one."""
    tokens = normalize_to_grammar(parse_document(_HOSTILE_FIXTURE))

    for token in _all_tokens(tokens):
        if token.type == "link_open":
            href = token.attrGet("href")
            if isinstance(href, str) and href:
                scheme = href.split(":", 1)[0]
                assert scheme in {"http", "https", "mailto"}


# --- AC-28's heading clamp and the emitted-as-text rule for out-of-grammar tokens -------------------

_DEEP_HEADINGS_FIXTURE = "#### Four\n\n##### Five\n\n###### Six\n"


def test_headings_deeper_than_three_are_clamped_to_three() -> None:
    tokens = normalize_to_grammar(parse_document(_DEEP_HEADINGS_FIXTURE))

    heading_opens = [t for t in tokens if t.type == "heading_open"]
    assert heading_opens, "the fixture must still produce at least one heading"
    for heading in heading_opens:
        assert heading.tag in {"h1", "h2", "h3"}


_OUT_OF_GRAMMAR_FIXTURE = (
    "| a | b |\n|---|---|\n| one | two |\n\n"
    "```\na code fence\n```\n\n"
    "> a blockquote\n\n"
    "---\n\n"
    "`inline code`\n\n"
    "~~strikethrough~~\n"
)


def test_table_fence_blockquote_hr_and_code_produce_no_tokens_of_their_own_type() -> None:
    """A table, fence, blockquote, hr or code token type must never reach a walker — outside the
    grammar is text (ADR-0017 §2 row 2, X-56)."""
    tokens = normalize_to_grammar(parse_document(_OUT_OF_GRAMMAR_FIXTURE))

    token_types = {token.type for token in _all_tokens(tokens)}
    for forbidden in (
        "table_open",
        "table_close",
        "fence",
        "blockquote_open",
        "blockquote_close",
        "hr",
        "code_inline",
        "code_block",
    ):
        assert forbidden not in token_types


def test_table_fence_blockquote_hr_and_code_survive_as_literal_text() -> None:
    """ "Emitted as text" means the content is not silently dropped — the PDF must still show the
    user what they wrote, just not as markup (X-56)."""
    tokens = normalize_to_grammar(parse_document(_OUT_OF_GRAMMAR_FIXTURE))

    rendered = "".join(token.content for token in _all_tokens(tokens) if token.content)
    assert "one" in rendered
    assert "two" in rendered
    assert "a code fence" in rendered
    assert "a blockquote" in rendered
    assert "inline code" in rendered
    assert "strikethrough" in rendered


# --- strip_refused_link_markup: pinned directly, both halves of X-9 / X-52 (MAJOR 1, /verify 1.5) --
#
# `strip_refused_link_markup` had no test of its own before this file — only three transitive
# `javascript:` cases in `test_plain_text.py`, `test_html.py` and `test_docx.py`, which cannot
# discriminate "refuse a URL" from "strip anything that merely looks like `[x](y)`". Measured
# end-to-end through `parse_document -> normalize_to_grammar -> render_plain_text`:
#
#     'Negotiated salary range [100k](150k)'  ->  'Negotiated salary range 100k'
#     'Refactored array[0](index) lookups'    ->  'Refactored array0 lookups'
#     'Cited [1](note) in the report'         ->  'Cited 1 in the report'
#
# The rule this table encodes comes from X-9 and X-52, not from the code above: a destination with
# **no scheme at all** (`note`, `150k`, `index`, `b`) was never a URL attempt — markdown-it's own
# link rule refused it before this helper ever runs, on the same footing as any other syntax it does
# not recognise — so the author's literal characters must survive byte-identical. A destination that
# **has** a scheme the allow-list refuses (`javascript:`, `data:`, `file:`, `vbscript:`), or is
# protocol-relative (no scheme under `urlsplit`, and the wire test below distinguishes it from a bare
# word only by the fixture, not the rule), is a refused *URL* and must be reduced to its label alone.
#
# `_allow_three_schemes` cannot currently tell these two apart — it answers `False` for both "no
# scheme" and "a disallowed scheme" — so `strip_refused_link_markup` conflates them today. The two
# tables below are written from the rule above, not from a read-back of the shipped behaviour, which
# is exactly why the second one is red on arrival.

# --- the stripping half: an actually-refused URL loses its destination, keeps its label -----------

_REFUSED_SCHEME_LITERALS = [
    ("[click me](javascript:alert(1))", "click me"),
    ("[x](data:text/html,%3Cb%3E)", "x"),
    ("[secret](file:///etc/passwd)", "secret"),
    ("[x](vbscript:msgbox(1))", "x"),
    ("[evil](//evil.example.com/cv.pdf)", "evil"),
    ("[x](ftp://example.com/cv.pdf)", "x"),
]

_REFUSED_SCHEME_IDS = [
    "javascript_stripped",
    "data_stripped",
    "file_stripped",
    "vbscript_stripped",
    "protocol_relative_stripped",
    "unknown_scheme_stripped",
]


@pytest.mark.parametrize(("literal", "expected"), _REFUSED_SCHEME_LITERALS, ids=_REFUSED_SCHEME_IDS)
def test_strip_refused_link_markup_reduces_a_refused_scheme_to_its_label(
    literal: str, expected: str
) -> None:
    assert strip_refused_link_markup(literal) == expected


# --- the narrow half: nothing currently pins this, and it is where the bug lives -------------------

_NO_SCHEME_LITERALS = [
    "Cited [1](note) in the report",
    "Negotiated salary range [100k](150k)",
    "Refactored array[0](index) lookups",
    "[a](b)",
]

_NO_SCHEME_IDS = [
    "citation_note",
    "salary_range",
    "array_index",
    "minimal_no_scheme",
]


@pytest.mark.parametrize("literal", _NO_SCHEME_LITERALS, ids=_NO_SCHEME_IDS)
def test_strip_refused_link_markup_leaves_a_no_scheme_destination_byte_identical(
    literal: str,
) -> None:
    """A destination with no scheme at all was never a URL attempt, so the label-only reduction the
    refused-scheme table above exercises must NOT apply here — the literal must come back exactly as
    written, brackets, parenthesised text and all. A widening of `_LINK_MARKUP` that started matching
    more destinations, or a narrowing of `_allow_three_schemes` that started treating "no scheme" the
    same as "refused scheme", must fail this test."""
    assert strip_refused_link_markup(literal) == literal


# --- the drive-letter half: a one-character "scheme" is not a URL attempt at all -------------------
#
# `urlsplit("C:/Users/me/cv.docx").scheme == "c"`, so today this reduces to "docs" exactly like a
# genuinely refused URL — there was no test pinning either answer, so the behaviour was an accident
# rather than a decision. Pinned here per the reviewer's argument at /verify slice 1.5 (iteration 2),
# which the harm is asymmetric on: over-stripping **deletes the author's characters** (MAJOR 1's own
# harm, in the table above); under-stripping merely shows inert text the parser already refused, with
# **zero** security consequence, because by the time this helper runs the link is a `text` token and
# there is no `href` left to exploit. No IANA-registered scheme is a single character, so a
# one-character scheme is a drive letter, not a URL — `_is_refused_url` needs a `len(scheme) > 1`
# guard, and this table is red until it has one.


def test_strip_refused_link_markup_leaves_a_windows_path_byte_identical() -> None:
    """A Windows path in a CV (`C:/Users/me/cv.docx`) must not be treated as a refused URL scheme —
    `urlsplit` parses the drive letter as `scheme="c"`, which is one character, and no IANA-registered
    scheme is. Red until `_is_refused_url` gains a `len(scheme) > 1` guard."""
    literal = "[docs](C:/Users/me/cv.docx)"

    assert strip_refused_link_markup(literal) == literal


def test_strip_refused_link_markup_still_strips_a_genuine_multi_character_refused_scheme() -> None:
    """The other edge of the same guard, in the same table: `len(scheme) > 1` must not be so wide
    that it lets a real refused scheme back through. This is what keeps the drive-letter allowance
    from being widened into uselessness — already green, and must stay green once the guard lands."""
    assert strip_refused_link_markup("[click me](javascript:alert(1))") == "click me"
    assert strip_refused_link_markup("[x](vbscript:msgbox(1))") == "x"


_ACCEPTED_SCHEME_LITERALS = [
    "See [example](https://example.com) for more.",
    "Email [me](mailto:person@example.com) directly.",
]

_ACCEPTED_SCHEME_IDS = ["https_untouched", "mailto_untouched"]


@pytest.mark.parametrize("literal", _ACCEPTED_SCHEME_LITERALS, ids=_ACCEPTED_SCHEME_IDS)
def test_strip_refused_link_markup_leaves_an_accepted_scheme_literal_untouched(
    literal: str,
) -> None:
    """The only way such a literal reaches this helper is that the author escaped the brackets
    (`\\[text\\](https://example.com)`) and meant to see them — the module's own docstring."""
    assert strip_refused_link_markup(literal) == literal


# --- end to end: the walker's own call to the helper, not just the helper in isolation -------------

_END_TO_END_NO_SCHEME_DOCUMENTS = [
    (
        "Negotiated salary range [100k](150k) after the offer.\n",
        "Negotiated salary range [100k](150k) after the offer.",
    ),
    (
        "Refactored array[0](index) lookups for the migration.\n",
        "Refactored array[0](index) lookups for the migration.",
    ),
    (
        "Cited [1](note) in the report.\n",
        "Cited [1](note) in the report.",
    ),
]

_END_TO_END_IDS = ["salary_range_end_to_end", "array_index_end_to_end", "citation_end_to_end"]


@pytest.mark.parametrize(
    ("markdown", "expected"), _END_TO_END_NO_SCHEME_DOCUMENTS, ids=_END_TO_END_IDS
)
def test_render_plain_text_preserves_a_no_scheme_bracket_pair_through_the_full_pipeline(
    markdown: str, expected: str
) -> None:
    """End to end through `parse_document -> normalize_to_grammar -> render_plain_text`, so the
    walker's own call to `strip_refused_link_markup` is covered, not only the helper called
    directly. `[100k](150k)` is never a link attempt — `validateLink` refuses `150k` for having no
    scheme, the same answer it gives `javascript:alert(1)` — but the two are not the same kind of
    refusal, and only one of them may destroy the author's text (MAJOR 1, /verify slice 1.5)."""
    tokens = normalize_to_grammar(parse_document(markdown))

    text = render_plain_text(tokens)

    assert text == expected
