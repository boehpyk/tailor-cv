"""`render_plain_text` — the `txt` walker. ADR-0017 §3.

ADR-0015 left one question open: is "plain text" the stored Markdown, or a marker-stripped
rendering? ADR-0017 closed it as the second. A plain-text CV is what a person pastes into an
application form's textarea, and `**` and `##` in that box read as broken — the base CV's own
extracted text (slice 1.1) never had them either. Markdown is the format for whoever wants the
source, which is the other half of why `txt` does not have to be it.

The line the rendering draws, and it is a deliberate one (AC-9, X-9):

- **Goes:** `#`, `**`, `*`, and the `[text](url)` syntax. There is nothing HTML-shaped to strip —
  `html=False` already turned raw markup into a text token upstream, so it arrives as its own
  literal characters and is *supposed* to survive as text.
- **Stays:** `- ` for a bullet and `1.` for an ordered item. **They are plain-text structure, not
  markup.** A list pasted without its markers is a paragraph. The "plain" in plain text is about
  markup, not about structure.
- A heading's text sits on its own line; a link renders as `text (https://…)`, or as the text alone
  when the scheme was refused upstream (X-9); a hard break is a newline; blocks are separated by one
  blank line.

**SKELETON (I1).** The body raises `NotImplementedError`; I3 fills it in after `qa` has recorded the
red. Red-first though it is infrastructure, for `tokens.py`'s reason: the contract above comes from
the spec, not from a library, so there is nothing to discover by writing it first.

**Pure, and it consumes the *normalized* stream** — `normalize_to_grammar` has already clamped the
headings and turned every foreign token into text, so this walker handles the eleven token types of
the grammar and nothing else. It never imports `html.py`: the two renderings agree about a link
because they have the same input, not because they share a constant (ADR-0017 §4).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final
from urllib.parse import urlsplit

from markdown_it.token import Token

from tailorcraft.infrastructure.export.tokens import (
    ALLOWED_LINK_SCHEMES,
    strip_refused_link_markup,
)

# Two spaces of indent per level of list nesting. A tab would be rendered by whatever textarea the
# user pastes into at whatever width that box feels like; spaces look the same everywhere.
_NESTED_LIST_INDENT: Final[str] = "  "

_LIST_OPEN_TYPES: Final[frozenset[str]] = frozenset({"bullet_list_open", "ordered_list_open"})
_LIST_CLOSE_TYPES: Final[frozenset[str]] = frozenset({"bullet_list_close", "ordered_list_close"})


def render_plain_text(tokens: Sequence[Token]) -> str:
    """Walk the normalized token stream into marker-stripped plain text."""
    blocks = _render_blocks(tokens)
    # An empty block would turn the single blank line between blocks into two. Headings and
    # paragraphs can both be empty — a document ending in `##` with nothing after it is one.
    return "\n\n".join(block for block in blocks if block)


def _render_blocks(tokens: Sequence[Token]) -> list[str]:
    """Each top-level block becomes one string; the caller joins them with one blank line."""
    blocks: list[str] = []
    index = 0

    while index < len(tokens):
        token = tokens[index]
        if token.type == "heading_open":
            text, index = _render_leaf_block(tokens, index + 1, "heading_close")
            blocks.append(text)
        elif token.type == "paragraph_open":
            text, index = _render_leaf_block(tokens, index + 1, "paragraph_close")
            blocks.append(text)
        elif token.type in _LIST_OPEN_TYPES:
            # A whole list is ONE block: its items are lines inside it, separated by a single
            # newline, and only the list as a whole gets a blank line before the next block.
            lines, index = _render_list(tokens, index, depth=0)
            blocks.append("\n".join(lines))
        elif token.type == "inline":
            blocks.append(_render_inline(token.children or []))
            index += 1
        elif token.type == "text":
            # Normalization turns a token outside the grammar into a bare block-level text token.
            blocks.append(strip_refused_link_markup(token.content))
            index += 1
        else:
            index += 1

    return blocks


def _render_leaf_block(tokens: Sequence[Token], index: int, close_type: str) -> tuple[str, int]:
    """Render the inline content of a heading or a paragraph; return it and the index after it."""
    parts: list[str] = []
    while index < len(tokens) and tokens[index].type != close_type:
        token = tokens[index]
        if token.type == "inline":
            parts.append(_render_inline(token.children or []))
        elif token.type == "text":
            parts.append(strip_refused_link_markup(token.content))
        index += 1
    return "".join(parts), index + 1


def _render_list(tokens: Sequence[Token], start: int, depth: int) -> tuple[list[str], int]:
    """Render one list, from its `*_list_open` to its matching close, as a list of lines.

    The markers are the whole point of AC-9's "bullets and numbers stay": `- ` and `1.` are
    plain-text *structure*, and a list pasted into an application form without them is a paragraph.
    """
    opener = tokens[start]
    ordered = opener.type == "ordered_list_open"
    number = _ordered_list_start(opener)
    indent = _NESTED_LIST_INDENT * depth
    lines: list[str] = []
    index = start + 1

    while index < len(tokens):
        token = tokens[index]
        if token.type in _LIST_CLOSE_TYPES:
            return lines, index + 1
        if token.type != "list_item_open":
            index += 1
            continue

        marker = f"{number}. " if ordered else "- "
        number += 1
        item_lines, index = _render_list_item(tokens, index + 1, depth)
        if item_lines:
            # The marker belongs to the item's first line; everything the item continues with —
            # a hard break, a second paragraph, a nested list — is already indented for its depth.
            lines.append(f"{indent}{marker}{item_lines[0]}")
            lines.extend(item_lines[1:])
        else:
            lines.append(f"{indent}{marker.rstrip()}")

    return lines, index


def _render_list_item(tokens: Sequence[Token], index: int, depth: int) -> tuple[list[str], int]:
    """Render one list item's contents as lines; return them and the index after `list_item_close`."""
    lines: list[str] = []
    continuation_indent = _NESTED_LIST_INDENT * (depth + 1)

    while index < len(tokens):
        token = tokens[index]
        if token.type == "list_item_close":
            return lines, index + 1
        if token.type in _LIST_OPEN_TYPES:
            nested_lines, index = _render_list(tokens, index, depth + 1)
            lines.extend(nested_lines)
            continue
        if token.type in ("paragraph_open", "heading_open"):
            close_type = token.type.replace("_open", "_close")
            text, index = _render_leaf_block(tokens, index + 1, close_type)
            new_lines = text.split("\n")
            lines.append(new_lines[0] if not lines else f"{continuation_indent}{new_lines[0]}")
            lines.extend(f"{continuation_indent}{line}" for line in new_lines[1:])
            continue
        index += 1

    return lines, index


def _ordered_list_start(opener: Token) -> int:
    """`1.` unless the author started the list somewhere else — markdown-it puts that on `start`."""
    start = opener.attrGet("start")
    if isinstance(start, int):
        return start
    if isinstance(start, str) and start.isdigit():
        return int(start)
    return 1


def _render_inline(children: Sequence[Token]) -> str:
    """Render one inline stream: markers go, text stays, a link becomes `text (url)`.

    `strong`/`em` contribute nothing at all — in markdown-it both come out of the one `emphasis`
    rule, so dropping these four token types is what "bold and italic markers are stripped" means.
    """
    parts: list[str] = []
    # One entry per open link: the URL to print after its text, or None when the scheme is refused.
    # A stack rather than a variable because nothing guarantees a stream has no nested `link_open`,
    # and a walker that assumes otherwise loses text on the day something does.
    link_urls: list[str | None] = []

    for token in children:
        match token.type:
            case "text":
                parts.append(strip_refused_link_markup(token.content))
            case "softbreak" | "hardbreak":
                # A hard break is a line break; so is a soft one here. Plain text has no way to say
                # "the author wrapped this line but meant one paragraph", and keeping the author's
                # own line structure is the friendlier answer for something pasted into a form.
                parts.append("\n")
            case "link_open":
                link_urls.append(_printable_link_url(token))
            case "link_close":
                url = link_urls.pop() if link_urls else None
                if url is not None:
                    parts.append(f" ({url})")
            case "inline":
                parts.append(_render_inline(token.children or []))
            case _:
                # `strong_open`, `strong_close`, `em_open`, `em_close` — the markers AC-9 drops.
                continue

    return "".join(parts)


def _printable_link_url(token: Token) -> str | None:
    """The URL to print after a link's text, or `None` when it must not be printed at all (X-52)."""
    href = token.attrGet("href")
    if isinstance(href, str) and href and _has_allowed_scheme(href):
        return href
    return None


def _has_allowed_scheme(url: str) -> bool:
    """A second check of the parse gate's rule, because a walker may be handed a foreign stream.

    `parse_document` cannot produce a `link_open` with a refused scheme, but this function is
    specified against *a* normalized stream, and the day a second producer appears (an importer, a
    different parser) is the day "the URL never reaches the rendering" has to be true here too.
    """
    try:
        return urlsplit(url).scheme in ALLOWED_LINK_SCHEMES
    except ValueError:
        return False
