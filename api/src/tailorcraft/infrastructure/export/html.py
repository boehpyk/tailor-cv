"""`render_html` and `sanitize_html` — the PDF's intermediate, and the second lock. ADR-0017 §2.

This is the first place in the codebase where a stranger's text becomes an **HTML string**. Slice
1.4 could argue itself out of a sanitizer — the editor goes Markdown → tokens → ProseMirror nodes →
`toDOM()`, so no HTML string ever exists on the client and there is nothing for a sanitizer to
sanitize (ADR-0015 §5). That argument does not survive a PDF: WeasyPrint takes an HTML *string*,
parses it, and resolves CSS against it.

**`nh3` is the second lock, not the first, and that is load-bearing.** With `html=False` and a
closed rule set, the emitter upstream can only produce the eleven tags on the allow-list, so on the
honest path `sanitize_html` removes nothing. It exists for the day someone enables a rule, upgrades
the parser, or adds a second writer — the model is already one, and an importer could be another.
Which is why AC-29 feeds it **hand-built hostile HTML past the parser**, through the adapter's
`sanitize` seam, and proves it holds on its own. A test that only ever fed it the emitter's output
would pass for the wrong reason forever, which is the same defect as a gate that checks nothing.

**The HTML never leaves the adapter's frame** (ADR-0017 §6). It is a local for the duration of one
`write_pdf()`: not stored, not logged, not returned, not named in `DocumentRendererPort`'s
signature. `render(markdown, *, document, format) -> bytes` mentions no HTML, no CSS and no page
size — "Markdown" there is the domain's own format, whereas HTML is one adapter's intermediate for
one format, and it stays inside.

The three constants below — the tag allow-list, the attribute allow-list and the URL schemes — are
the whole of `sanitize_html`'s policy, written as data so AC-29 can assert over them directly rather
than infer them from behaviour.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape as escape_html
from typing import Final, assert_never
from urllib.parse import urlsplit

import nh3
from markdown_it.token import Token

from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.export.tokens import strip_refused_link_markup

# The allow-list, pinned by AC-29 exactly as `nh3.clean` takes it — as data, which is the reason
# ADR-0017 chose `nh3` (Rust's `ammonia`) over a hand-rolled stripper: HTML sanitization is a genre
# with a CVE history, and `nh3` parses rather than pattern-matches.
#
# Eleven tags: the closed document grammar and nothing else. Anything the emitter cannot produce is
# also anything this will not pass.
ALLOWED_TAGS: Final[set[str]] = {
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

# Two tags carry an attribute, and no tag carries `style` or anything matching `on*` — which is not
# an exclusion written here but the consequence of an allow-list: nh3 drops every attribute that is
# not named. `start` on `ol` is what keeps "4." reading as 4 in a list that does not begin at 1.
ALLOWED_ATTRIBUTES: Final[dict[str, set[str]]] = {
    "a": {"href", "title"},
    "ol": {"start"},
}

# The same three schemes `tokens._allow_three_schemes` enforces at the parse, enforced again here.
# Two gates on one rule is the point (X-52): this one has to hold even if the first is ever
# reconfigured, so it is a second copy on purpose rather than a shared constant that a single edit
# could widen in both places at once.
ALLOWED_URL_SCHEMES: Final[set[str]] = {"http", "https", "mailto"}

# The three heading tags the emitter is allowed to write. Not a fourth copy of the grammar — it is
# the subset of `ALLOWED_TAGS` a token's own `tag` field could ever reach the output through, and
# `_heading_tag` uses it to keep "only the eleven tags" true for any input.
_HEADING_TAGS: Final[frozenset[str]] = frozenset({"h1", "h2", "h3"})


def render_html(tokens: Sequence[Token], document: TailoredDocumentKind) -> str:
    """Emit the normalized token stream as an HTML document for WeasyPrint.

    A constant `<!doctype html>` shell around the fragment; `<title>` is a constant per document
    kind, **never** the source's first line (X-55's reasoning, one layer up — no user text ever
    reaches a place where it could be mistaken for metadata).
    """
    return wrap_in_document(render_body_fragment(tokens), document)


def render_body_fragment(tokens: Sequence[Token]) -> str:
    """The `<body>` contents alone — **this is what goes through `sanitize_html`**, not the document.

    Measured, not assumed, and it is the one composition trap in this module. `html`, `head`,
    `title` and `body` are not on `ALLOWED_TAGS` — an allow-list of a *document grammar* has no
    reason to contain them — so `sanitize_html(render_html(...))` deletes the shell and keeps the
    title's **text**, and the PDF comes out with a stray line reading "Tailored CV" above the name.
    The technical plan's step 3 writes the call that way in one sentence and describes the shell as
    wrapping "the sanitized fragment" in the next; the second sentence is the correct one.

    So the adapter composes `wrap_in_document(sanitize(render_body_fragment(tokens)), document)` —
    sanitize the fragment, then wrap. `render_html` is that composition without the sanitize seam,
    which is what its own test pins and what a caller wants when it is not passing a `sanitize`.
    """
    return _render_tokens(tokens)


def wrap_in_document(fragment: str, document: TailoredDocumentKind) -> str:
    """The constant `<!doctype html>` shell. Nothing from the document decides anything in it."""
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{_document_title(document)}</title></head><body>"
        f"{fragment}"
        f"</body></html>"
    )


def _document_title(document: TailoredDocumentKind) -> str:
    """One constant per document kind, closed by `assert_never`.

    The technical plan pinned `Tailored CV` and never named the cover letter's. It was decided at
    I2/I3 — **`Cover Letter`** — and this comment is where that decision lives, so the next reader
    finds a choice that was made rather than a string someone invented. It is deliberately not
    `Tailored Cover Letter`: the title is what a PDF reader puts in its window chrome, and "tailored"
    is our word for the process, not the reader's word for the document.

    A `match` closed by `assert_never`, so a third document kind fails to type-check here rather
    than rendering a PDF titled after the wrong one.
    """
    match document:
        case TailoredDocumentKind.CV:
            return "Tailored CV"
        case TailoredDocumentKind.COVER_LETTER:
            return "Cover Letter"
    assert_never(document)


def _render_tokens(tokens: Sequence[Token]) -> str:
    """Walk the block level of a normalized stream. Only the eleven allowed tags can come out.

    That is a property of this function and not only of the stream it is given: every tag below is
    a literal, and the one place a token's own `tag` would reach the output — a heading — is clamped
    on the way past. `normalize_to_grammar` owns the clamp for the pipeline; this keeps the
    emitter's own promise true for any stream anybody ever hands it.
    """
    parts: list[str] = []

    for token in tokens:
        match token.type:
            case "heading_open":
                parts.append(f"<{_heading_tag(token)}>")
            case "heading_close":
                parts.append(f"</{_heading_tag(token)}>\n")
            case "paragraph_open":
                # A paragraph inside a list item is `hidden`: markdown-it's way of saying the item
                # is tight and the text belongs directly inside the `<li>`.
                if not token.hidden:
                    parts.append("<p>")
            case "paragraph_close":
                if not token.hidden:
                    parts.append("</p>\n")
            case "bullet_list_open":
                parts.append("<ul>\n")
            case "bullet_list_close":
                parts.append("</ul>\n")
            case "ordered_list_open":
                parts.append(_ordered_list_open(token))
            case "ordered_list_close":
                parts.append("</ol>\n")
            case "list_item_open":
                parts.append("<li>")
            case "list_item_close":
                parts.append("</li>\n")
            case "inline":
                parts.append(_render_inline(token.children or []))
            case _:
                # A block-level `text` token — what normalization leaves behind for a token type
                # outside the grammar — and the floor for anything else, which can only be text.
                parts.append(_escape_text(token.content))

    return "".join(parts)


def _heading_tag(token: Token) -> str:
    return token.tag if token.tag in _HEADING_TAGS else "h3"


def _ordered_list_open(token: Token) -> str:
    """`<ol>`, or `<ol start="4">` when the author did not begin at 1 — the one `ol` attribute."""
    start = token.attrGet("start")
    if isinstance(start, int) or (isinstance(start, str) and start.isdigit()):
        return f'<ol start="{int(start)}">\n'
    return "<ol>\n"


def _render_inline(children: Sequence[Token]) -> str:
    """Walk one inline stream. Text is escaped; a link is emitted only with an allowed scheme."""
    parts: list[str] = []
    # One entry per open link: True when its `<a>` was emitted and its `</a>` must be too. A stack,
    # because an unbalanced or nested stream must not be able to leave a dangling close tag.
    open_anchors: list[bool] = []

    for token in children:
        match token.type:
            case "text":
                parts.append(_escape_text(token.content))
            case "softbreak":
                parts.append("\n")
            case "hardbreak":
                parts.append("<br />\n")
            case "strong_open":
                parts.append("<strong>")
            case "strong_close":
                parts.append("</strong>")
            case "em_open":
                parts.append("<em>")
            case "em_close":
                parts.append("</em>")
            case "link_open":
                anchor = _anchor_open(token)
                open_anchors.append(anchor is not None)
                if anchor is not None:
                    parts.append(anchor)
            case "link_close":
                emitted = open_anchors.pop() if open_anchors else False
                if emitted:
                    parts.append("</a>")
            case "inline":
                parts.append(_render_inline(token.children or []))
            case _:
                parts.append(_escape_text(token.content))

    return "".join(parts)


def _anchor_open(token: Token) -> str | None:
    """`<a href="…">` for an allowed scheme; `None` when the link keeps its text and loses its tag.

    X-52's expected result for the PDF is "the link text with no `href`", and dropping the whole
    anchor is how that is delivered — `nh3` would strip the attribute a moment later anyway, and an
    `<a>` with nothing to point at is a tag with no meaning left in it.
    """
    href = token.attrGet("href")
    if not isinstance(href, str) or not href or not _has_allowed_scheme(href):
        return None
    title = token.attrGet("title")
    attributes = f' href="{escape_html(href, quote=True)}"'
    if isinstance(title, str) and title:
        attributes += f' title="{escape_html(title, quote=True)}"'
    return f"<a{attributes}>"


def _has_allowed_scheme(url: str) -> bool:
    try:
        return urlsplit(url).scheme in ALLOWED_URL_SCHEMES
    except ValueError:
        return False


def _escape_text(text: str) -> str:
    """Every character of the document that reaches the HTML goes through here.

    `strip_refused_link_markup` first, so a link the parser refused shows the reader its text rather
    than the `javascript:` URL they would otherwise see spelled out in their own PDF (X-52); then
    `escape`, which is what makes `<script>` in a CV a visible five-character word instead of a tag
    for `nh3` to have an opinion about (X-51).
    """
    return escape_html(strip_refused_link_markup(text), quote=False)


def sanitize_html(html: str) -> str:
    """The second lock: `nh3.clean` on the grammar's allow-list (AC-29).

    The exact call I3 writes: `tags=ALLOWED_TAGS`, `attributes=ALLOWED_ATTRIBUTES`,
    `url_schemes=ALLOWED_URL_SCHEMES`, `link_rel="noopener noreferrer"`, `strip_comments=True`.
    Also the adapter's testing seam — `MarkdownDocumentRenderer(..., sanitize=...)` defaults to this
    function and nothing under `api/src/` ever passes the argument.

    Five keyword arguments and no sixth. `clean_content_tags` is left at nh3's own default
    (`script`, `style`), which is the one place a *dropped* tag's text content must go with it: the
    body of a `<script>` is code, not something a reader is missing.

    **Feed it `render_body_fragment`'s output, never `render_html`'s** — see that function for what
    happens to the shell otherwise.
    """
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )
