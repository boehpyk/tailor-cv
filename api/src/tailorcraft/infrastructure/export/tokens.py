"""The parse and the normalization — ADR-0017 §1 and obligations 1 and 2 of its §2.

Everything downstream of this module trusts its output. `render_plain_text`, `render_docx` and
`render_html` each walk the *normalized* token stream, so one normalization protects three walkers
and a hostile token cannot reach any of them. That is the whole shape of the pipeline: the security
decision happens once, at the parse, and the three renderers are ordinary code.

**Pure.** The standard library, `markdown_it`, and nothing else — no settings, no clock, no I/O, no
database, no network. That purity is why AC-28 can be a table of hostile fixtures that runs in
microseconds, and it is worth defending: the moment this module needs a `Settings` it has stopped
being the thing a table can exhaustively cover.

**SKELETON (I1).** The three function bodies raise `NotImplementedError`; I3 fills them in after
`qa` has recorded the red. This module is red-first *even though it is infrastructure*, which is a
deliberate exception to the tier table in docs/sdlc.md §2 — the same exception `posting/
address_policy.py` took in slice 1.2 and `llm/parsing.py` took in 1.3, for the same reason. The
test-after tiers are the ones whose shape is *discovered against a library*; the contract here is
fixed by the specification in advance (AC-28, X-51 … X-53, X-56), so it is written down as tests
first. `GRAMMAR_RULES`, by contrast, is written whole below: a constant **is** its value, so there
is no body for a red to discriminate against, and its test is green on arrival.

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
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

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


def parse_document(markdown: str) -> Sequence[Token]:
    """Parse stored Markdown into the token stream the three walkers consume.

    Obligation 1 of ADR-0017 §2: `MarkdownIt("zero", {"html": False})`, exactly `GRAMMAR_RULES`
    enabled, `validateLink` replaced by `_allow_three_schemes`.
    """
    raise NotImplementedError


def normalize_to_grammar(tokens: Sequence[Token]) -> Sequence[Token]:
    """Clamp a parsed stream into the closed grammar the three walkers are allowed to assume.

    Obligation 2 of ADR-0017 §2: `h4`+ becomes `h3`; a token type outside the grammar becomes a
    `text` token carrying its content; list nesting is bounded. A rendering bound, not a security
    one — the security happened at the parse.
    """
    raise NotImplementedError


def _allow_three_schemes(url: str) -> bool:
    """Replace markdown-it's `validateLink`: `http`, `https` and `mailto` — nothing else.

    The URL is judged by `urllib.parse.urlsplit(url).scheme`, so a protocol-relative `//host` has
    no scheme and is refused along with `javascript:`, `data:`, `file:` and `vbscript:` (X-52).
    A refused link keeps its *text* and loses its `href`; nothing about the document disappears.

    Private by name because nothing outside this module calls it in production — it is handed to
    the parser — but AC-28 tests it directly as the table it is.
    """
    raise NotImplementedError
