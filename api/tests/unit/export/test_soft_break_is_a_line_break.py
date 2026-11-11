"""AC-13: a CommonMark soft break is a line break in every export (ADR-0017's 2026-11-10 amendment).

Pure: the real parse and normalization, then each walker. Expectations are typed from the spec, not
read off the output. `SOFT_BREAK_FIXTURE_MARKDOWN` has single-newline lines the parser reports as
`softbreak`; today the walkers turn them into a space (HTML, DOCX) or leave them to the browser.
"""

from __future__ import annotations

import re
from io import BytesIO

from docx import Document

from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.export.docx import render_docx
from tailorcraft.infrastructure.export.html import render_body_fragment
from tailorcraft.infrastructure.export.plain_text import render_plain_text
from tailorcraft.infrastructure.export.tokens import normalize_to_grammar, parse_document
from tests.fixtures.documents import SOFT_BREAK_FIXTURE_MARKDOWN

# Single-newline joints in the fixture: 1 under the h1 (an ATX heading is one line), 2 in the role, 1 mid-sentence.
_SOFT_BREAKS = 4


def _types(markdown: str) -> list[str]:
    return [
        child.type
        for token in normalize_to_grammar(parse_document(markdown))
        for child in (token.children or [])
    ]


def test_the_fixture_really_parses_with_soft_breaks() -> None:
    """Guards the other tests: a fixture the parser reads without soft breaks proves nothing."""
    raw = [c.type for t in parse_document(SOFT_BREAK_FIXTURE_MARKDOWN) for c in (t.children or [])]

    assert raw.count("softbreak") == _SOFT_BREAKS


def test_normalize_to_grammar_never_yields_a_softbreak() -> None:
    assert "softbreak" not in _types(SOFT_BREAK_FIXTURE_MARKDOWN)


def test_normalize_to_grammar_yields_a_hardbreak_for_each_single_newline() -> None:
    assert _types(SOFT_BREAK_FIXTURE_MARKDOWN).count("hardbreak") == _SOFT_BREAKS


def test_pdf_html_has_a_br_between_the_header_lines() -> None:
    html = render_body_fragment(normalize_to_grammar(parse_document(SOFT_BREAK_FIXTURE_MARKDOWN)))

    # The `<br />` is what AC-13 asks for; the whitespace after it is the hardbreak arm's own
    # (`<br />\n`, unchanged since 1.5) and is not pinned.
    assert re.search(r"Senior Backend Engineer<br />\s*jordan@example\.com \| London", html)


def test_pdf_html_has_one_br_per_soft_break() -> None:
    html = render_body_fragment(normalize_to_grammar(parse_document(SOFT_BREAK_FIXTURE_MARKDOWN)))

    assert html.count("<br />") == _SOFT_BREAKS


def test_docx_has_a_line_break_run_for_each_soft_break() -> None:
    data = render_docx(
        normalize_to_grammar(parse_document(SOFT_BREAK_FIXTURE_MARKDOWN)),
        TailoredDocumentKind.CV,
    )

    body = Document(BytesIO(data)).element.body
    breaks = body.xpath(".//w:br")

    assert len(breaks) == _SOFT_BREAKS


def test_docx_header_lines_are_not_joined_by_a_space() -> None:
    data = render_docx(
        normalize_to_grammar(parse_document(SOFT_BREAK_FIXTURE_MARKDOWN)),
        TailoredDocumentKind.CV,
    )

    texts = [p.text for p in Document(BytesIO(data)).paragraphs]

    assert not any("Acme Corp 2021" in t or "Engineer Acme" in t for t in texts)
    # The fixture's role is one paragraph of three lines, so its DOCX paragraph is all three.
    assert any(t == "Senior Backend Engineer\nAcme Corp\n2021 - Present" for t in texts)


def test_txt_keeps_a_newline_between_the_role_lines() -> None:
    text = render_plain_text(normalize_to_grammar(parse_document(SOFT_BREAK_FIXTURE_MARKDOWN)))

    assert "Senior Backend Engineer\nAcme Corp\n2021 - Present" in text
