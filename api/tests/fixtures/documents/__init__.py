"""The Markdown fixture corpus for the export pipeline's tests (AC-47).

**Mirrored by hand from the editor's own corpus**,
`web/src/features/editor/markdown/fixtures.ts`, copied field for field rather than generated. AC-47
exists to prove the two bridges — the client's ProseMirror one and the server's `markdown-it-py`
one — agree about what a document *is*: every document the editor considers valid must render to
all four export formats without failing. Two corpora typed by hand and then compared is the whole
point; a shared file or a generator would hide the day they drift apart instead of failing a test
when they do.

Every fixture below is typed out from the TypeScript source's own literal strings, never produced
by running either pipeline and pasting its output — the same rule the TS module's docstring states
for itself.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarkdownFixture:
    name: str
    markdown: str


# Every grammar element in one document: headings 1-3, a paragraph, bold, italic, a link, a hard
# break (CommonMark's backslash-before-newline form), a bullet list and an ordered list.
GRAMMAR_FIXTURE_MARKDOWN = (
    "# Heading one\n"
    "\n"
    "## Heading two\n"
    "\n"
    "### Heading three\n"
    "\n"
    "A paragraph with **bold** and *italic* text, and a [link](https://example.com/apply).\n"
    "\n"
    "- First bullet\n"
    "- Second bullet\n"
    "\n"
    "1. First step\n"
    "2. Second step\n"
    "\n"
    "A line with a hard break\\\n"
    "that continues on the next line.\n"
)

# Raw HTML in the source. There is no HTML path here either (ADR-0017 §2 obligation 1): a
# `<script>` and an `<img onerror>` must come through as inert text.
RAW_HTML_FIXTURE_MARKDOWN = (
    "# Profile\n"
    "\n"
    "<script>alert(1)</script>\n"
    "\n"
    "<img src=x onerror=alert(1)>\n"
    "\n"
    "Regular paragraph text after the hostile lines.\n"
)

# A link whose scheme is refused outright — never a followable, scripted `href` (X-52).
JAVASCRIPT_LINK_FIXTURE_MARKDOWN = "Click [x](javascript:alert(1)) to continue reading."

# A model-shaped CV — the kind of document the LLM actually returns.
MODEL_CV_FIXTURE_MARKDOWN = (
    "# Jordan Rivera\n"
    "\n"
    "## Experience\n"
    "\n"
    "**Senior Backend Engineer**, Acme Corp\n"
    "\n"
    "- Led the migration to a *hexagonal* architecture\n"
    "- Reduced p95 latency by 40% across the payments service\n"
    "\n"
    "**Backend Engineer**, Widgets Inc\n"
    "\n"
    "- Owned the on-call rotation for the billing pipeline\n"
    "\n"
    "## Education\n"
    "\n"
    "### BSc Computer Science\n"
    "\n"
    "[University website](https://example.edu)\n"
)

# A link whose text is identical to its `href` — the common LinkedIn/GitHub/portfolio line shape.
SELF_DESCRIBING_LINK_FIXTURE_MARKDOWN = (
    "See [https://example.com/x](https://example.com/x) for more."
)

# A model-shaped cover letter — the second document every succeeded run carries.
MODEL_LETTER_FIXTURE_MARKDOWN = (
    "# Cover Letter\n"
    "\n"
    "Dear Hiring Manager,\n"
    "\n"
    "I am writing to apply for the **Senior Backend Engineer** role at Acme Corp. My experience "
    "leading\n"
    "a *hexagonal* migration maps directly onto the responsibilities in your posting.\n"
    "\n"
    "1. Ownership of a production payments pipeline\n"
    "2. A track record of measurable latency work\n"
    "\n"
    "Sincerely,\n"
    "Jordan Rivera\n"
)

# A source certain to normalize on the way through a round trip (`__bold__` -> `**bold**`).
NORMALIZATION_FIXTURE_MARKDOWN = "A paragraph with __bold__ text that should normalize.\n"

# A `[label](destination)` shape whose destination has no scheme at all — never a link attempt, and
# never reachable by `_allow_three_schemes` as anything but "not allowed" (MAJOR 1, /verify slice
# 1.5). `strip_refused_link_markup` must leave this byte-identical: "$100k" written by an author as
# "[100k](150k)" is a salary range, not a refused URL, and "[1](note)" is a citation, not an attack.
NO_SCHEME_BRACKET_FIXTURE_MARKDOWN = (
    "## Compensation\n"
    "\n"
    "Negotiated salary range [100k](150k) after the offer, cited as [1](note) in the report.\n"
)

# The full corpus AC-47's every-format-renders test iterates — the same eight documents the
# editor's own `markdownFixtureCorpus` exports, in the same order.
MARKDOWN_FIXTURE_CORPUS: tuple[MarkdownFixture, ...] = (
    MarkdownFixture("every grammar element", GRAMMAR_FIXTURE_MARKDOWN),
    MarkdownFixture("raw HTML (script and an onerror image)", RAW_HTML_FIXTURE_MARKDOWN),
    MarkdownFixture("a javascript: link", JAVASCRIPT_LINK_FIXTURE_MARKDOWN),
    MarkdownFixture("a model-shaped CV", MODEL_CV_FIXTURE_MARKDOWN),
    MarkdownFixture("a model-shaped cover letter", MODEL_LETTER_FIXTURE_MARKDOWN),
    MarkdownFixture("a [url](url) self-describing link", SELF_DESCRIBING_LINK_FIXTURE_MARKDOWN),
    MarkdownFixture("certain normalisation (__bold__ -> **bold**)", NORMALIZATION_FIXTURE_MARKDOWN),
    MarkdownFixture(
        "a non-URL bracket-paren pair (salary range, citation)", NO_SCHEME_BRACKET_FIXTURE_MARKDOWN
    ),
)
