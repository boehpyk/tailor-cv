"""`DraftTextAdapter` (slice 5.2, T30, test-after): the draft CV as the TXT download would read it.

The expected strings are typed from AC-9's table (`tests/unit/export/test_plain_text.py`), the TXT
export's contract, never read back off the adapter.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from tailorcraft.infrastructure.scoring import draft_text
from tailorcraft.infrastructure.scoring.draft_text import DraftTextAdapter


async def test_markdown_comes_back_as_marker_free_plain_text() -> None:
    markdown = (
        "# Jane Doe\n\n"
        "**Senior** engineer, *remote*.\n\n"
        "## Experience\n\n"
        "- Built the billing ledger\n"
        "- Ran [the platform](https://example.com/p)\n"
    )

    text = await DraftTextAdapter().to_plain_text(markdown)

    assert text == (
        "Jane Doe\n\n"
        "Senior engineer, remote.\n\n"
        "Experience\n\n"
        "- Built the billing ledger\n"
        "- Ran the platform (https://example.com/p)"
    )


async def test_an_empty_draft_is_empty_text_not_an_error() -> None:
    assert await DraftTextAdapter().to_plain_text("") == ""


async def test_the_conversion_runs_off_the_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parsing is synchronous and CPU-bound: inline it would stall every request (CLAUDE.md, async)."""
    seen: list[int] = []
    original = draft_text._to_plain_text

    def spy(markdown: str) -> str:
        seen.append(threading.get_ident())
        return original(markdown)

    monkeypatch.setattr(draft_text, "_to_plain_text", spy)
    loop_thread = threading.get_ident()

    await asyncio.wait_for(DraftTextAdapter().to_plain_text("# A\n"), timeout=10)

    assert seen
    assert seen[0] != loop_thread
