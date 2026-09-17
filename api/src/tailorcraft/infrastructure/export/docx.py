"""`render_docx` — the DOCX walker. ADR-0017 §4, AC-31.

The third walker over the same normalized token stream, and the one that never touches HTML.
`python-docx` has no HTML importer and should not grow one here: going through the HTML would put
a third consumer behind the sanitizer and make a *rendering* decision depend on a *security* stage.
An import-graph test asserts that no module in `infrastructure/export/` both imports `docx` and
imports the HTML module (AC-31), which is why this module carries its own copy of the two document
titles rather than importing `html._document_title`. If one of those strings ever changes, the other
has to change with it — that is the price of the ban, stated here so the next reader finds a
duplication that was chosen rather than one that was missed.

**It consumes the *normalized* stream**, which is the whole reason this walker can be ordinary code.
`normalize_to_grammar` has already clamped `h4`+ to `h3`, replaced every foreign token type with
text and bounded list nesting, so a hostile token cannot reach `python-docx` any more than it can
reach the HTML emitter. One normalization protects three walkers (ADR-0017 §2).

Three things are measured rather than assumed, against **python-docx 1.2.0**:

1. **Links become `text (url)`.** `python-docx` exposes no hyperlink API without hand-built OXML,
   and a CV's links are few (OQ-10). The plain-text rendering made the same choice, and the two
   agree because they have the same input rather than because they share a constant (ADR-0017 §4).
   A link whose scheme the parser refused survives in the stream as the literal `[label](javascript:…)`
   — see `tokens.py`'s module docstring for why — so `strip_refused_link_markup` runs on every text
   this module writes. X-52's DOCX row is "the text alone, no URL", and that helper is what delivers
   it here exactly as it does for TXT and PDF.
2. **XML forbids most control characters, and `lxml` enforces it.** A `\x0b`, `\x0c` or `\x01` in the
   source reaches this walker intact — CommonMark replaces only NUL, measured against markdown-it-py
   4.2.0 — and `run.text = "…\x0b…"` raises `ValueError: All strings must be XML compatible` from
   inside `lxml`. That would make a document which renders fine as PDF, TXT and Markdown fail
   permanently as DOCX, with `render_failed` and nothing in the log saying why. Control characters
   that XML does not allow are therefore dropped here (`\t`, `\n` and `\r` stay, and `python-docx`
   turns the last two into `<w:br/>` and `<w:tab/>` itself). This is a *format* constraint, not a
   sanitization step: DOCX is XML and the other three formats are not.
3. **The built-in list styles stop at the spec's second level.** The default template does ship
   `List Bullet 3` and `List Number 3`, but the plan names `List Bullet` / `List Bullet 2` /
   `List Number` / `List Number 2` and nothing deeper, so an item nested deeper than that keeps the
   level-2 style rather than growing a suffix the spec never chose. `normalize_to_grammar` already
   bounds nesting at six, so this clamp only ever affects how deep the indent *looks*.

Two known limits of the built-in styles, neither worth hand-built OXML in this slice and both
invisible in the token stream: every `List Number` paragraph in a document shares the template's one
numbering definition (`numId 5`), so two separate ordered lists continue one sequence in Word rather
than restarting; and an `<ol start="4">` — which the HTML emitter does honour — cannot be expressed
through a style at all, so a list that starts at 4 in the PDF starts at 1 in the DOCX.
"""

from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO
from typing import Final, assert_never
from urllib.parse import urlsplit

from docx import Document as new_document
from docx.document import Document
from docx.exceptions import PythonDocxError
from docx.opc.exceptions import OpcError
from docx.oxml.exceptions import XmlchemyError
from docx.text.paragraph import Paragraph
from markdown_it.token import Token

from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.export.tokens import (
    ALLOWED_LINK_SCHEMES,
    strip_refused_link_markup,
)

# `python-docx`'s own exception types that can escape this module, measured against **python-docx
# 1.2.0** by sweeping the package for `Exception` subclasses. They are the *specific* translations
# that sit on top of `MarkdownDocumentRenderer`'s floor, carrying the better reason (`render_failed`,
# the document's own fault) instead of the residual `render_error`.
#
# They live here rather than in the adapter because `renderer.py` must not import `docx` — it
# imports the HTML module, and AC-31 forbids any module from doing both. So the adapter catches this
# tuple by name and the vendor stays behind its own walker.
#
# Three base classes, not the ten leaves: `InvalidSpanError` and `InvalidXmlError` are
# `PythonDocxError`s, `PackageNotFoundError` is an `OpcError`, and `oxml`'s own `InvalidXmlError` is
# an `XmlchemyError`. The `docx.image.exceptions` family is deliberately absent — every one of them
# is raised while *inserting an image*, and an image is unrepresentable in this pipeline (X-53), so
# listing them would be a translation for a path that cannot be reached.
DOCX_DOCUMENT_ERRORS: Final[tuple[type[Exception], ...]] = (
    PythonDocxError,
    OpcError,
    XmlchemyError,
)

# The default template's own heading styles, by the level the grammar allows. `normalize_to_grammar`
# clamps `h4`+ to `h3`, so a fourth entry here would be unreachable; `_heading_style` clamps again
# anyway, because this walker is specified against *a* normalized stream and the day a second
# producer appears is the day that has to hold here too.
_HEADING_STYLES: Final[dict[str, str]] = {
    "h1": "Heading 1",
    "h2": "Heading 2",
    "h3": "Heading 3",
}

# Depth 0 and depth 1+; see the module docstring for why this stops at 2 rather than 3.
_BULLET_STYLES: Final[tuple[str, ...]] = ("List Bullet", "List Bullet 2")
_NUMBER_STYLES: Final[tuple[str, ...]] = ("List Number", "List Number 2")

_LIST_OPEN_TYPES: Final[frozenset[str]] = frozenset({"bullet_list_open", "ordered_list_open"})
_LIST_CLOSE_TYPES: Final[frozenset[str]] = frozenset({"bullet_list_close", "ordered_list_close"})

# The characters XML 1.0 forbids outright: C0 except tab, newline and carriage return, plus DEL and
# the C1 block. Built as a translation table once, because this runs over every character of the
# document.
_XML_FORBIDDEN: Final[dict[int, None]] = dict.fromkeys(
    [*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), *range(0x7F, 0xA0)]
)


def render_docx(tokens: Sequence[Token], document: TailoredDocumentKind) -> bytes:
    """Walk the normalized token stream into a DOCX file's bytes.

    Synchronous and CPU-bound, like every renderer here; `MarkdownDocumentRenderer` runs it in
    `asyncio.to_thread` under `asyncio.wait_for`, never on the event loop.
    """
    docx_document = new_document()
    # The title only when the author's own document does not open with one. A CV whose first line is
    # the person's name as an H1 must not get "Tailored CV" stamped above it — that is the same
    # mistake the PDF's `<title>` composition made before I3 measured it.
    if not _contains_h1(tokens):
        docx_document.add_paragraph(_document_title(document), style=_HEADING_STYLES["h1"])

    _render_blocks(docx_document, tokens)

    buffer = BytesIO()
    docx_document.save(buffer)
    return buffer.getvalue()


def _document_title(document: TailoredDocumentKind) -> str:
    """The two constants, closed by a `match` so a third document kind fails to type-check.

    A deliberate second copy of `html._document_title`'s strings; AC-31 forbids this module from
    importing that one. See the module docstring.
    """
    match document:
        case TailoredDocumentKind.CV:
            return "Tailored CV"
        case TailoredDocumentKind.COVER_LETTER:
            return "Cover Letter"
    assert_never(document)


def _contains_h1(tokens: Sequence[Token]) -> bool:
    """Does the source open a heading at level 1 anywhere? Then it supplies its own title."""
    return any(token.type == "heading_open" and token.tag == "h1" for token in tokens)


def _render_blocks(docx_document: Document, tokens: Sequence[Token]) -> None:
    """Walk the block level. Every branch ends in a paragraph, because DOCX has nothing else.

    `docx.Document` is a *factory function*, not the class; the class it returns lives in
    `docx.document`. Both are imported above — the factory under the name it is called by, the class
    under the name it is annotated by — so that these signatures say what they hold rather than
    taking an untyped `object` and reaching through it.
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type == "heading_open":
            paragraph = _paragraph(docx_document, _heading_style(token))
            index = _fill_leaf_block(paragraph, tokens, index + 1, "heading_close")
        elif token.type == "paragraph_open":
            paragraph = _paragraph(docx_document, None)
            index = _fill_leaf_block(paragraph, tokens, index + 1, "paragraph_close")
        elif token.type in _LIST_OPEN_TYPES:
            index = _render_list(docx_document, tokens, index, depth=0)
        elif token.type == "inline":
            _fill_inline(_paragraph(docx_document, None), token.children or [])
            index += 1
        elif token.type == "text":
            # Normalization leaves a token outside the grammar behind as a bare block-level `text`.
            _add_text_run(_paragraph(docx_document, None), token.content, bold=0, italic=0)
            index += 1
        else:
            index += 1


def _paragraph(docx_document: Document, style: str | None) -> Paragraph:
    """Add one paragraph, with a built-in style name or the template's `Normal`."""
    return docx_document.add_paragraph(style=style)


def _heading_style(token: Token) -> str:
    """`Heading 1` to `Heading 3`, clamped for any stream — see `_HEADING_STYLES`."""
    return _HEADING_STYLES.get(token.tag, _HEADING_STYLES["h3"])


def _list_style(ordered: bool, depth: int) -> str:
    """`List Bullet`/`List Number`, with the level-2 suffix from depth 1 down."""
    styles = _NUMBER_STYLES if ordered else _BULLET_STYLES
    return styles[min(depth, len(styles) - 1)]


def _render_list(docx_document: Document, tokens: Sequence[Token], start: int, depth: int) -> int:
    """Render one list, from its `*_list_open` to its matching close; return the index after it."""
    ordered = tokens[start].type == "ordered_list_open"
    index = start + 1

    while index < len(tokens):
        token = tokens[index]
        if token.type in _LIST_CLOSE_TYPES:
            return index + 1
        if token.type != "list_item_open":
            index += 1
            continue
        index = _render_list_item(docx_document, tokens, index + 1, depth, ordered)

    return index


def _render_list_item(
    docx_document: Document, tokens: Sequence[Token], index: int, depth: int, ordered: bool
) -> int:
    """Render one item's contents; return the index after its `list_item_close`.

    Every paragraph the item contains takes the list style, continuations included — a second
    paragraph inside a bullet is rare in a CV, and giving it the same style keeps it indented with
    the bullet it belongs to instead of escaping to the left margin.
    """
    style = _list_style(ordered, depth)
    emitted = False

    while index < len(tokens):
        token = tokens[index]
        if token.type == "list_item_close":
            if not emitted:
                # An item with no content still has to exist, or a round trip counts fewer bullets
                # than the author wrote.
                _paragraph(docx_document, style)
            return index + 1
        if token.type in _LIST_OPEN_TYPES:
            index = _render_list(docx_document, tokens, index, depth + 1)
            continue
        if token.type in ("paragraph_open", "heading_open"):
            close_type = token.type.replace("_open", "_close")
            index = _fill_leaf_block(
                _paragraph(docx_document, style), tokens, index + 1, close_type
            )
            emitted = True
            continue
        index += 1

    return index


def _fill_leaf_block(
    paragraph: Paragraph, tokens: Sequence[Token], index: int, close_type: str
) -> int:
    """Fill a heading's or a paragraph's runs; return the index after its closing token."""
    while index < len(tokens) and tokens[index].type != close_type:
        token = tokens[index]
        if token.type == "inline":
            _fill_inline(paragraph, token.children or [])
        elif token.type == "text":
            _add_text_run(paragraph, token.content, bold=0, italic=0)
        index += 1
    return index + 1


def _fill_inline(paragraph: Paragraph, children: Sequence[Token]) -> None:
    """Walk one inline stream into runs: `strong`/`em` become run flags, a link becomes `text (url)`.

    `bold` and `italic` are **counters, not booleans**. Nothing guarantees a stream has no nested
    `strong_open`, and a boolean would let the inner close turn the outer emphasis off for the rest
    of the paragraph.
    """
    bold = 0
    italic = 0
    # One entry per open link: the URL to write after its text, or `None` when the scheme was
    # refused. A stack for the same reason `plain_text` keeps one — an unbalanced or nested stream
    # must not lose text or print somebody else's URL.
    link_urls: list[str | None] = []

    for token in children:
        match token.type:
            case "text":
                _add_text_run(paragraph, token.content, bold=bold, italic=italic)
            case "softbreak":
                # A space, not a break. The DOCX is a rendered document like the PDF, where a soft
                # break collapses to whitespace; `plain_text` keeps the author's line structure
                # instead because a textarea has no other way to show it.
                _add_text_run(paragraph, " ", bold=bold, italic=italic)
            case "hardbreak":
                paragraph.add_run().add_break()
            case "strong_open":
                bold += 1
            case "strong_close":
                bold = max(bold - 1, 0)
            case "em_open":
                italic += 1
            case "em_close":
                italic = max(italic - 1, 0)
            case "link_open":
                link_urls.append(_printable_link_url(token))
            case "link_close":
                url = link_urls.pop() if link_urls else None
                if url is not None:
                    _add_text_run(paragraph, f" ({url})", bold=bold, italic=italic)
            case "inline":
                _fill_inline(paragraph, token.children or [])
            case _:
                continue


def _add_text_run(paragraph: Paragraph, text: str, *, bold: int, italic: int) -> None:
    """The one place document text becomes a run — so the two transformations happen exactly once.

    `strip_refused_link_markup` first (X-52's DOCX row: the label alone, never the refused URL),
    then the XML character filter (see the module docstring). An empty result adds no run at all,
    because an empty run is a node a round trip has to explain.
    """
    cleaned = strip_refused_link_markup(text).translate(_XML_FORBIDDEN)
    if not cleaned:
        return
    run = paragraph.add_run(cleaned)
    run.bold = bold > 0
    run.italic = italic > 0


def _printable_link_url(token: Token) -> str | None:
    """The URL to write after a link's text, or `None` when it must not be written at all (X-52).

    A second check of the parse gate's rule, for `plain_text._has_allowed_scheme`'s reason:
    `parse_document` cannot produce a `link_open` with a refused scheme, but this walker is
    specified against *a* normalized stream, and a second producer must not be able to get a
    `javascript:` URL typed into somebody's CV.
    """
    href = token.attrGet("href")
    if not isinstance(href, str) or not href:
        return None
    try:
        scheme = urlsplit(href).scheme
    except ValueError:
        # `urlsplit` raises on a malformed IPv6 literal such as `http://[`, and this runs over
        # whatever a stranger's document contains. Not printing it is the only answer.
        return None
    return href if scheme in ALLOWED_LINK_SCHEMES else None
