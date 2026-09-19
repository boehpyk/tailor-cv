"""`render_plain_text` — the marker-stripped `txt` walker (AC-9, ADR-0017 §3, X-9).

Pure test: no I/O, no event loop, no fixtures, no mocks. Every expected string below is typed out
from AC-9's table and ADR-0017 §3's prose, never read back off `render_plain_text`'s output — the
function raises `NotImplementedError` unconditionally at the time of writing (docs/sdlc.md §2), so
every assertion here is a red on that exception, not on a comparison the code already satisfies.

**Token fixtures are built by a small parser constructed *in this file*, independent of
`tokens.py::parse_document`.** `tokens.py` owns its own RED test (`test_tokens.py`) for the parse and
the normalization; `render_plain_text` is specified to consume *a* normalized token stream, and this
file's job is only to hand it valid ones. Building them locally means this file's reds are never
masked by `tokens.py` raising first — every fixture here that stays inside the closed grammar (no
heading past h3, no table/fence/etc.) is already in normalized form, since `normalize_to_grammar`
has nothing to do to it.

The `validateLink` replacement below is a second, independent typing of AC-28's three-scheme rule —
deliberately not imported from `tokens.py::_allow_three_schemes`, which also raises
`NotImplementedError` right now and would break every fixture that needs a working parser.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

from tailorcraft.infrastructure.export.plain_text import render_plain_text

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


def _validate_link(url: str) -> bool:
    return urlsplit(url).scheme in {"http", "https", "mailto"}


def _tokens(markdown: str) -> list[Token]:
    parser = MarkdownIt("zero", {"html": False}).enable(list(_GRAMMAR_RULES))
    parser.validateLink = _validate_link  # type: ignore[method-assign]
    return parser.parse(markdown)


# --- AC-9's table -------------------------------------------------------------------------------


def test_heading_loses_its_hash_marker() -> None:
    text = render_plain_text(_tokens("# Experience\n"))

    assert text == "Experience"


def test_bullet_items_render_with_a_dash_marker() -> None:
    text = render_plain_text(_tokens("- First job\n- Second job\n"))

    assert text == "- First job\n- Second job"


def test_ordered_items_render_with_their_number_and_a_dot() -> None:
    text = render_plain_text(_tokens("1. First job\n2. Second job\n"))

    assert text == "1. First job\n2. Second job"


def test_bold_and_italic_markers_are_stripped() -> None:
    text = render_plain_text(_tokens("**bold** and *italic* text\n"))

    assert text == "bold and italic text"


def test_link_renders_as_its_text_followed_by_the_url_in_parentheses() -> None:
    text = render_plain_text(_tokens("[TailorCraft](https://tailorcraft.example/cv)\n"))

    assert text == "TailorCraft (https://tailorcraft.example/cv)"


def test_link_with_a_disallowed_scheme_renders_as_the_text_alone_with_no_url() -> None:
    """X-9: the scheme was refused upstream, so the URL never reaches the plain-text rendering —
    only the link's own text survives, with no brackets and no parenthesised URL."""
    text = render_plain_text(_tokens("[click me](javascript:alert(1))\n"))

    assert text == "click me"


def test_hard_break_renders_as_a_newline() -> None:
    text = render_plain_text(_tokens("Line one  \nLine two\n"))

    assert text == "Line one\nLine two"


def test_blocks_are_separated_by_exactly_one_blank_line() -> None:
    text = render_plain_text(_tokens("# Heading\n\nA paragraph.\n\n- a bullet\n"))

    assert text == "Heading\n\nA paragraph.\n\n- a bullet"


def test_raw_html_in_the_source_appears_as_its_literal_text() -> None:
    """`html=False` already turned raw markup into a `text` token carrying its own angle brackets
    upstream — there is no HTML markup left in the stream for this walker to strip, so it is
    supposed to survive verbatim."""
    text = render_plain_text(_tokens("<b>looks bold</b> but is not\n"))

    assert text == "<b>looks bold</b> but is not"
