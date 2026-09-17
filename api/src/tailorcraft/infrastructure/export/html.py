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

**SKELETON (I1).** Both bodies raise `NotImplementedError`; I3 writes the emitter and the exact
`nh3.clean` call after `qa` has recorded the red. The three constants below are written whole, for
the reason `GRAMMAR_RULES` is: a constant is its value, so AC-29's assertions over them are green on
arrival and nothing was deferred. `nh3` is deliberately **not imported yet** — the import arrives
with the body it serves, in I3.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from markdown_it.token import Token

from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind

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


def render_html(tokens: Sequence[Token], document: TailoredDocumentKind) -> str:
    """Emit the normalized token stream as an HTML document for WeasyPrint.

    A constant `<!doctype html>` shell around the fragment; `<title>` is a constant per document
    kind, **never** the source's first line (X-55's reasoning, one layer up — no user text ever
    reaches a place where it could be mistaken for metadata).
    """
    raise NotImplementedError


def sanitize_html(html: str) -> str:
    """The second lock: `nh3.clean` on the grammar's allow-list (AC-29).

    The exact call I3 writes: `tags=ALLOWED_TAGS`, `attributes=ALLOWED_ATTRIBUTES`,
    `url_schemes=ALLOWED_URL_SCHEMES`, `link_rel="noopener noreferrer"`, `strip_comments=True`.
    Also the adapter's testing seam — `MarkdownDocumentRenderer(..., sanitize=...)` defaults to this
    function and nothing under `api/src/` ever passes the argument.
    """
    raise NotImplementedError
