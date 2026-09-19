"""`render_html` and `sanitize_html` — the PDF's intermediate and the second lock (AC-29, ADR-0017
§2 row 3).

Pure test: no I/O, no event loop, no fixtures, no mocks. Every expected value below is typed out
from AC-29's exact `nh3.clean` call and the technical plan's HTML shell, never read back off the
module under test. `render_html` and `sanitize_html` both raise `NotImplementedError`
unconditionally at the time of writing, so every behavioural assertion here reds on that exception —
the call reached the right function and refused.

`ALLOWED_TAGS`, `ALLOWED_ATTRIBUTES` and `ALLOWED_URL_SCHEMES` are written whole in the skeleton — a
constant is its value — so the tests over them are **green on arrival**.

`render_html`'s token fixtures are built by a small parser constructed *in this file*, independent
of `tokens.py::parse_document`, for the same reason `test_plain_text.py` builds its own: this file's
reds must come from `html.py`, never be pre-empted by `tokens.py` raising first.
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt
from markdown_it.token import Token

from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.export.html import (
    ALLOWED_ATTRIBUTES,
    ALLOWED_TAGS,
    ALLOWED_URL_SCHEMES,
    render_body_fragment,
    sanitize_html,
    wrap_in_document,
)

_GRAMMAR_RULES = (
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


def _tokens(markdown: str) -> list[Token]:
    parser = MarkdownIt("zero", {"html": False}).enable(list(_GRAMMAR_RULES))
    return parser.parse(markdown)


def _render_document(tokens: list[Token], document: TailoredDocumentKind) -> str:
    """`wrap_in_document(sanitize_html(render_body_fragment(tokens)), document)` — the exact
    composition `renderer.py` uses (`fragment = render_body_fragment(...)`, then
    `wrap_in_document(self._sanitize(fragment), document)`), not `render_html`'s own composition,
    which skips the sanitize seam and has zero production callers (I3's measured defect: it is what
    let `sanitize_html(render_html(...))` delete the document shell and keep the title's text).
    Re-pointing here so these three assertions exercise the composition a user can actually reach —
    coverage `render_html` never delivered, per /verify slice 1.5 (iteration 2, task 3)."""
    return wrap_in_document(sanitize_html(render_body_fragment(tokens)), document)


# --- The three constants: written whole in the skeleton, so these are green on arrival -------------
#
# Typed out from AC-29's exact `nh3.clean` call, never read back off the constants under test.

_EXPECTED_ALLOWED_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "ul",
    "ol",
    "li",
    "br",
    "strong",
    "em",
    "a",
}

_EXPECTED_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "ol": {"start"},
}

_EXPECTED_ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}


def test_allowed_tags_is_exactly_the_eleven_tag_grammar() -> None:
    assert ALLOWED_TAGS == _EXPECTED_ALLOWED_TAGS


def test_allowed_tags_is_a_set() -> None:
    assert isinstance(ALLOWED_TAGS, set)


def test_allowed_attributes_matches_the_pinned_call() -> None:
    assert ALLOWED_ATTRIBUTES == _EXPECTED_ALLOWED_ATTRIBUTES


def test_allowed_url_schemes_is_the_same_three_schemes_as_the_parser_gate() -> None:
    assert ALLOWED_URL_SCHEMES == _EXPECTED_ALLOWED_URL_SCHEMES


# --- render_body_fragment: only the eleven tags, on a full-grammar fixture, unmediated by nh3 -------

_FULL_GRAMMAR_FIXTURE = (
    "# Heading One\n\n"
    "## Heading Two\n\n"
    "### Heading Three\n\n"
    "#### Heading Four\n\n"
    "A paragraph with **bold** and *italic* text, a hard break  \n"
    "and a [link](https://example.com).\n\n"
    "- bullet one\n"
    "- bullet two\n\n"
    "1. first\n"
    "2. second\n"
)


def test_render_body_fragment_uses_only_the_eleven_allowed_tags() -> None:
    """The FIRST lock's own promise, unmediated by the second one.

    Found at `/verify` (slice 1.5, iteration 2): this test used to call `_render_document`, i.e.
    `wrap_in_document(sanitize_html(render_body_fragment(tokens)), document)`, and `sanitize_html`
    emits only `ALLOWED_TAGS` **by construction** against this same eleven-element set — so
    `found_tags <= _EXPECTED_ALLOWED_TAGS` could no longer fail for any emitter output at all.
    Measured: breaking `_heading_tag`'s clamp (`return token.tag`, unconditionally) still left this
    test green, because `sanitize_html` quietly deleted the leaked `<h4>` tag along with its markup.
    It had become a duplicate of `test_sanitize_html_strips_every_tag_outside_the_allow_list`, and the
    module's own docstring is explicit about exactly this trap: "`nh3` is the second lock, not the
    first... A test that only ever fed it the emitter's output would pass for the wrong reason
    forever." This was that sentence's mirror image — the first lock's only independent test, testing
    the second lock instead.

    So this calls `render_body_fragment` directly: no `wrap_in_document`, no `sanitize_html`, no
    `<body>` shell to slice out. The fixture carries a `####` heading (deeper than `_HEADING_TAGS`
    allows) so the emitter's own clamp — not `normalize_to_grammar`'s, which this file's local
    `_tokens()` never runs — is what a broken `_heading_tag` would leak past.
    """
    fragment = render_body_fragment(_tokens(_FULL_GRAMMAR_FIXTURE))

    found_tags = {tag.lower() for tag in re.findall(r"<\s*/?\s*([a-zA-Z0-9]+)", fragment)}
    assert found_tags, "the fixture must produce at least one tag in the fragment"
    assert found_tags <= _EXPECTED_ALLOWED_TAGS


# --- render_body_fragment + wrap_in_document: the shell `renderer.py` actually composes it into -----


def test_render_document_wraps_the_fragment_in_the_constant_document_shell() -> None:
    """The exact shell string from the technical plan's "The render, step by step" §3."""
    html = _render_document(_tokens("# Heading\n"), TailoredDocumentKind.CV)

    assert html.startswith(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        "<title>Tailored CV</title></head><body>"
    )
    assert html.endswith("</body></html>")


def test_render_document_title_is_a_constant_never_the_documents_own_first_line() -> None:
    """X-55, one layer up: a `Content-Disposition`-style injection attempt in the source must never
    end up somewhere the document could pass for metadata — the title is fixed per document kind,
    never derived from the text."""
    html = _render_document(
        _tokens('"; filename="evil.exe\n\nOther content.\n'), TailoredDocumentKind.CV
    )

    title_end = html.index("</title>")
    title_section = html[: title_end + len("</title>")]
    assert "evil.exe" not in title_section


# --- sanitize_html: hand-built hostile HTML fed *past* the parser (AC-29) --------------------------

_HOSTILE_HTML = (
    "<p>Hello <script>alert(1)</script> world</p>"
    '<img src="x" onerror="alert(1)">'
    '<iframe src="http://evil.example"></iframe>'
    '<div style="color:red" onclick="alert(1)">styled div</div>'
    '<a href="javascript:alert(1)">bad link</a>'
    '<a href="https://example.com" title="ok">good link</a>'
    "<!-- a sneaky comment -->"
)


def test_sanitize_html_strips_every_tag_outside_the_allow_list() -> None:
    result = sanitize_html(_HOSTILE_HTML)

    for forbidden_tag in ("script", "img", "iframe", "div"):
        assert f"<{forbidden_tag}" not in result


def test_sanitize_html_strips_on_star_attributes() -> None:
    result = sanitize_html(_HOSTILE_HTML)

    assert "onerror" not in result
    assert "onclick" not in result


def test_sanitize_html_strips_style_attributes() -> None:
    result = sanitize_html(_HOSTILE_HTML)

    assert "style=" not in result


def test_sanitize_html_removes_a_javascript_href() -> None:
    result = sanitize_html(_HOSTILE_HTML)

    assert "javascript:" not in result


def test_sanitize_html_strips_html_comments() -> None:
    result = sanitize_html(_HOSTILE_HTML)

    assert "a sneaky comment" not in result
    assert "<!--" not in result


def test_sanitize_html_adds_rel_noopener_noreferrer_to_a_surviving_link() -> None:
    result = sanitize_html('<a href="https://example.com">good link</a>')

    assert 'rel="noopener noreferrer"' in result
