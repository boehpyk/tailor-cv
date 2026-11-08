"""`DraftTextAdapter`: the `DraftTextPort` (slice 5.2, plan §0.8).

The TXT export's own pipeline (parse, normalize, `render_plain_text`), so the after-score judges the
draft exactly as a user's TXT download would read it. All three steps are synchronous and CPU-bound,
so they run in a thread: inline on the loop they would stall every request while one draft parses.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from tailorcraft.infrastructure.export.plain_text import render_plain_text
from tailorcraft.infrastructure.export.tokens import normalize_to_grammar, parse_document

if TYPE_CHECKING:
    from tailorcraft.domain.scoring.ports import DraftTextPort


def _to_plain_text(markdown: str) -> str:
    return render_plain_text(normalize_to_grammar(parse_document(markdown)))


class DraftTextAdapter:
    async def to_plain_text(self, markdown: str) -> str:
        return await asyncio.to_thread(_to_plain_text, markdown)


if TYPE_CHECKING:

    def _assert_implements_draft_text_port(adapter: DraftTextAdapter) -> None:
        _: DraftTextPort = adapter
