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

from tailorcraft.infrastructure.export.tokens import (
    GRAMMAR_RULES,
    _allow_three_schemes,
    normalize_to_grammar,
    parse_document,
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
