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

from markdown_it.token import Token


def render_plain_text(tokens: Sequence[Token]) -> str:
    """Walk the normalized token stream into marker-stripped plain text."""
    raise NotImplementedError
