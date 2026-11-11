"""The eval's layout counts and flags (AC-19), test-after: pure functions over strings, no I/O."""

from __future__ import annotations

from dataclasses import replace

from tailorcraft.infrastructure.llm.evaluation.checks import (
    FLAT_MIN_CHARACTERS,
    LayoutCounts,
    formatting_dropped,
    is_flat,
    layout_counts,
)

FORMATTED = """# Ada Example
London · ada@example.com
07700 900123

## Experience

**Engineer** — Northwind Widgets
- Built the thing
- Ran the other thing
1. Numbered item
"""


BASE = LayoutCounts(
    lines=20,
    blocks=5,
    headings=0,
    bold_spans=0,
    soft_breaks=0,
    longest_line=100,
    characters=FLAT_MIN_CHARACTERS,
)


def counts(**overrides: int) -> LayoutCounts:
    return replace(BASE, **overrides)


def test_layout_counts_a_formatted_cv() -> None:
    assert layout_counts(FORMATTED) == LayoutCounts(
        lines=8,
        blocks=3,
        headings=2,
        bold_spans=1,
        # Only "07700…" after "London…". The line after a heading is not inside a paragraph, and
        # "- Built" after the bold line is a list item, which opens a block.
        soft_breaks=1,
        longest_line=len("**Engineer** — Northwind Widgets"),
        characters=len(FORMATTED),
    )


def test_layout_counts_an_empty_document() -> None:
    assert layout_counts("") == LayoutCounts(0, 0, 0, 0, 0, 0, 0)


def test_a_single_line_cv_is_flat() -> None:
    one_line = "word " * 400
    assert is_flat(layout_counts(one_line))


def test_flat_needs_the_length_floor() -> None:
    assert not is_flat(counts(lines=1, characters=FLAT_MIN_CHARACTERS - 1))
    assert is_flat(counts(lines=1, characters=FLAT_MIN_CHARACTERS))


def test_flat_on_too_few_lines() -> None:
    assert is_flat(counts(lines=7))
    assert not is_flat(counts(lines=8))


def test_flat_on_one_line_over_half_the_document() -> None:
    assert not is_flat(counts(longest_line=750, characters=1_500))
    assert is_flat(counts(longest_line=751, characters=1_500))


def test_formatting_dropped_only_when_the_base_had_some_and_the_tailored_has_none() -> None:
    plain = counts()
    assert formatting_dropped(counts(headings=1), plain)
    assert formatting_dropped(counts(bold_spans=1), plain)
    assert not formatting_dropped(counts(headings=1), counts(bold_spans=1))
    assert not formatting_dropped(counts(bold_spans=2), counts(headings=1))
    assert not formatting_dropped(plain, plain)
