"""The parse and the normalization — ADR-0017 §1 and obligations 1 and 2 of its §2.

Everything downstream of this module trusts its output. `render_plain_text`, `render_docx` and
`render_html` each walk the *normalized* token stream, so one normalization protects three walkers
and a hostile token cannot reach any of them. That is the whole shape of the pipeline: the security
decision happens once, at the parse, and the three renderers are ordinary code.

**Pure.** The standard library, `markdown_it`, and nothing else — no settings, no clock, no I/O, no
database, no network. That purity is why AC-28 can be a table of hostile fixtures that runs in
microseconds, and it is worth defending: the moment this module needs a `Settings` it has stopped
being the thing a table can exhaustively cover.

This module was built **red-first even though it is infrastructure**, a deliberate exception to the
tier table in docs/sdlc.md §2 — the same exception `posting/address_policy.py` took in slice 1.2 and
`llm/parsing.py` took in 1.3, for the same reason. The test-after tiers are the ones whose shape is
*discovered against a library*; the contract here was fixed by the specification in advance (AC-28,
X-51 … X-53, X-56), so it was written down as tests first. `GRAMMAR_RULES` is the exception to the
exception: a constant **is** its value, so there was no body for a red to discriminate against.

The two obligations this module owns, and the line between them:

1. **`parse_document` is the first lock.** `MarkdownIt("zero", {"html": False})` with exactly
   `GRAMMAR_RULES` enabled means raw HTML is never markup — it becomes a `text` token carrying its
   own angle brackets (X-51) — `image` is not an enabled rule, so an image is *unrepresentable*
   rather than removed (X-53), and `_allow_three_schemes` replaces markdown-it's own
   `validateLink`, so a `javascript:`, `data:`, `file:` or protocol-relative URL never becomes an
   `href` at all (X-52). It is not a sanitizer of HTML it did not produce; that is `html.py`'s job,
   and it is the *second* lock (ADR-0017 §2).
2. **`normalize_to_grammar` is the rendering half, not a security boundary.** It is what makes the
   PDF match what the user saw in the editor: `h4`+ clamped to `h3`, every token type outside the
   closed grammar replaced by text, list nesting bounded (X-56). 1.4's editor applies the same rule
   on the client; this is the server's copy of it, and the clamp is a pure function either side can
   be tested against.

**What markdown-it actually does with a refused URL, which is not what the plan assumed.** The I1
skeleton (and the technical plan's step 1) described a refused link as one that "keeps its text and
loses its `href`". Measured against the installed markdown-it-py 4.2.0, that shape does not exist:
when `validateLink` returns `False` the *whole link rule fails*, so `[click me](javascript:alert(1))`
is not a `link_open` without an attribute — it is a single `text` token carrying the literal
characters `[click me](javascript:alert(1))`, brackets, URL and all. Safe (it is text, and nothing
renders it as markup), but it is not what X-9 and X-52 promise the user: "the text alone, no URL".
`strip_refused_link_markup` below is where that promise is kept, and the two walkers that render
human-facing text call it. The spec won over the parser's default; the parser's behaviour is
recorded here so the next reader does not "fix" the helper away.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

# The rules enabled on top of markdown-it's `zero` preset — the closed document grammar, and the
# whole of it. `zero` enables nothing but the bare minimum, so this list is not a set of
# *additions* to a sensible default: it is the entire surface the parser is allowed to recognise.
#
# Written whole in the skeleton on purpose. A constant is its value, the way a dataclass's field
# list is its signature; there is no body here for a RED test to discriminate against, so AC-28's
# assertion over this list is green the moment the file exists. `domain/export/events.py` was
# written whole in T1 for exactly this reason. Read that green as "nothing was deferred", never as
# a skipped red.
#
# What is deliberately ABSENT matters more than what is present, so each omission has a reason:
#   - `image`      — an image is unrepresentable, not removed (X-53). Nothing can ever be fetched
#                    for a document, because no token can ever ask for a fetch.
#   - `html_block` /
#     `html_inline` — with `html=False` these never fire anyway; not enabling them is the belt to
#                    that option's braces (X-51).
#   - `table`, `fence`, `code`, `blockquote`, `hr`, `strikethrough` — outside the editor's grammar
#                    (ADR-0015), so they render as their literal text and the PDF matches the
#                    editor (X-56).
#   - `linkify`    — not installed and not enabled. Auto-linking bare text would manufacture an
#                    `href` the author never wrote, which is precisely the thing
#                    `_allow_three_schemes` exists to control.
#
# This is not a setting and there is no way to widen it at runtime. A knob on an allow-list is an
# off switch (ADR-0017 §5).
GRAMMAR_RULES: Final[tuple[str, ...]] = (
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

# The three schemes a link may carry, in the module that owns the parse gate. `html.py` keeps its
# own copy for `nh3` on purpose (a second lock that a single edit must not be able to widen in both
# places at once); this one is the *parser's* rule, and the two walkers that render a link for a
# human read it from here rather than typing a third copy.
ALLOWED_LINK_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https", "mailto"})

# The closed set of token types the three walkers are allowed to see. Everything else becomes text.
#
# It is written as the set of types the enabled rules can actually emit — nine block types, the
# `inline` container, and the seven inline types — rather than as a list of things to exclude. That
# direction is the point: a rule enabled tomorrow adds a type that is *not* on this list, so it
# arrives at the walkers as harmless text instead of as an unhandled `match` arm.
GRAMMAR_TOKEN_TYPES: Final[frozenset[str]] = frozenset(
    {
        "paragraph_open",
        "paragraph_close",
        "heading_open",
        "heading_close",
        "bullet_list_open",
        "bullet_list_close",
        "ordered_list_open",
        "ordered_list_close",
        "list_item_open",
        "list_item_close",
        "inline",
        "text",
        "softbreak",
        "hardbreak",
        "strong_open",
        "strong_close",
        "em_open",
        "em_close",
        "link_open",
        "link_close",
    }
)

# The three heading levels the editor's grammar has. `h4`+ clamps to `h3` (X-56).
_ALLOWED_HEADING_TAGS: Final[frozenset[str]] = frozenset({"h1", "h2", "h3"})

# How deep a list may nest before further nesting flattens into the deepest allowed level. A render
# bound, not a security one: a 40-deep list is a page of whitespace and a stack of `<ul>`s, not an
# attack. Deeper items keep their text and join the depth-6 list.
MAX_LIST_DEPTH: Final[int] = 6

_LIST_OPEN_TYPES: Final[frozenset[str]] = frozenset({"bullet_list_open", "ordered_list_open"})
_LIST_CLOSE_TYPES: Final[frozenset[str]] = frozenset({"bullet_list_close", "ordered_list_close"})

# The residue a refused link leaves behind: `[label](destination)` as literal text. The destination
# alternation allows one level of balanced parentheses, exactly as markdown-it's own link
# destination parser does, because `javascript:alert(1)` is the shape this exists for.
#
# Both branches of that alternation start with a different character (`(` versus anything but), so
# the pattern is unambiguous and cannot backtrack catastrophically — this runs over text a stranger
# supplied, so that property is checked rather than assumed.
_LINK_MARKUP: Final[re.Pattern[str]] = re.compile(
    r"\[(?P<label>[^\[\]]*)\]\((?P<destination>(?:[^()\s]|\([^()\s]*\))*)\)"
)


def parse_document(markdown: str) -> Sequence[Token]:
    """Parse stored Markdown into the token stream the three walkers consume.

    Obligation 1 of ADR-0017 §2: `MarkdownIt("zero", {"html": False})`, exactly `GRAMMAR_RULES`
    enabled, `validateLink` replaced by `_allow_three_schemes`.

    A parser per call, deliberately. It is a few microseconds against a render measured in
    milliseconds, and a module-level parser would be shared mutable state across two processes'
    threads — `MarkdownIt` carries per-parse state and is not documented as thread-safe, and this
    function is called from a thread pool in both the API and the worker.
    """
    parser = MarkdownIt("zero", {"html": False}).enable(list(GRAMMAR_RULES))
    # Assigning over the bound method is markdown-it's own documented seam for the link gate; mypy
    # calls every method assignment an error, and this one is the library's API.
    parser.validateLink = _allow_three_schemes  # type: ignore[method-assign]
    return parser.parse(markdown)


def normalize_to_grammar(tokens: Sequence[Token]) -> Sequence[Token]:
    """Clamp a parsed stream into the closed grammar the three walkers are allowed to assume.

    Obligation 2 of ADR-0017 §2: `h4`+ becomes `h3`; a token type outside the grammar becomes a
    `text` token carrying its content; list nesting is bounded. A rendering bound, not a security
    one — the security happened at the parse.

    **This one function is what protects all three walkers.** `render_plain_text`, `render_docx` and
    `render_html` each handle the twenty types of `GRAMMAR_TOKEN_TYPES` and nothing else, which is
    only safe because nothing else can reach them — so a hostile token cannot arrive at
    `python-docx` any more than it can arrive at the HTML emitter.

    Pure: the input tokens are never mutated. A token that needs changing is replaced by a copy, so
    a caller that kept a reference to the parsed stream still holds the parsed stream.
    """
    normalized: list[Token] = []
    open_lists = 0
    # Lists whose `*_open` was dropped for depth. Their `*_close` must be dropped too, and the
    # stream is properly nested, so a counter is enough: the next close always belongs to the
    # innermost still-open list.
    dropped_lists = 0

    for token in tokens:
        if token.type in _LIST_OPEN_TYPES:
            if open_lists >= MAX_LIST_DEPTH:
                dropped_lists += 1
                continue
            open_lists += 1
            normalized.append(token)
        elif token.type in _LIST_CLOSE_TYPES:
            if dropped_lists > 0:
                dropped_lists -= 1
                continue
            open_lists = max(open_lists - 1, 0)
            normalized.append(token)
        elif token.type in ("heading_open", "heading_close"):
            normalized.append(_clamped_heading(token))
        elif token.type == "inline":
            children = list(normalize_to_grammar(token.children or []))
            normalized.append(token.copy(children=children))
        elif token.type in GRAMMAR_TOKEN_TYPES:
            normalized.append(token)
        else:
            normalized.append(_as_text(token))

    return normalized


def _clamped_heading(token: Token) -> Token:
    """`h4`, `h5`, `h6` — and anything else a future rule invents — become `h3` (X-56)."""
    if token.tag in _ALLOWED_HEADING_TAGS:
        return token
    # `markup` carries the `####` the author typed, and both the open and the close token hold it.
    # It is not rendered anywhere today, but leaving it disagreeing with `tag` would be a small lie
    # waiting for the first walker that reads it.
    return token.copy(tag="h3", markup="###")


def _as_text(token: Token) -> Token:
    """Replace a token whose type is outside the grammar with a `text` token carrying its content.

    "Emitted as text" means the user still sees what they wrote — the PDF must not silently lose a
    paragraph because a rule we do not support produced it (X-56). An `*_open` token has no content
    of its own, so its `markup` (`>`, ```` ``` ````, `~~`) is the literal the reader typed; a
    matching `*_close` contributes nothing, or the markup would appear twice.
    """
    if token.content:
        literal = token.content
    elif token.type.endswith("_open"):
        literal = token.markup
    else:
        literal = ""
    return Token(type="text", tag="", nesting=0, content=literal, level=token.level)


def strip_refused_link_markup(text: str) -> str:
    """Recover the label from the literal `[label](destination)` a refused link leaves behind.

    See this module's docstring: markdown-it does not hand back a link without an `href` when
    `validateLink` refuses a URL — it hands back the author's literal characters. X-9 and X-52 both
    say what the reader must get in that case ("the text alone, no URL"), so the walkers that
    render for a human call this on every text token.

    Narrow on purpose. A destination whose scheme the parser *would* have accepted is left exactly
    as it is, because the only way such a literal reaches the stream is that the author escaped it
    (`\\[not a link\\](https://example.com)`) and meant to see it.
    """

    def replace(match: re.Match[str]) -> str:
        if _allow_three_schemes(match["destination"]):
            return match[0]
        return match["label"]

    return _LINK_MARKUP.sub(replace, text)


def _allow_three_schemes(url: str) -> bool:
    """Replace markdown-it's `validateLink`: `http`, `https` and `mailto` — nothing else.

    The URL is judged by `urllib.parse.urlsplit(url).scheme`, so a protocol-relative `//host` has
    no scheme and is refused along with `javascript:`, `data:`, `file:` and `vbscript:` (X-52).
    A refused link keeps its *text* and loses its `href`; nothing about the document disappears.

    Private by name because nothing outside this module calls it in production — it is handed to
    the parser — but AC-28 tests it directly as the table it is.

    **It never raises.** markdown-it calls it on whatever a stranger typed between `(` and `)`, and
    `urlsplit` raises `ValueError` on a malformed IPv6 literal such as `http://[`. A refusal is the
    only answer this function is allowed to give to input it cannot read.
    """
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        return False
    return scheme in ALLOWED_LINK_SCHEMES
