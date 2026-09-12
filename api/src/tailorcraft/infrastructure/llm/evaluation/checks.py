"""The cheap, objective half of the prompt eval: pure functions with no I/O, no adapter and no clock.

ADR-0004: prompt quality is judgement, and dressing judgement up as a passing test is how you ship a
confident, useless feature. So this module holds only what can honestly be decided by a machine, and
every heuristic in it states what it cannot see.

**The employer heuristic, and its limits, stated plainly.** Every corpus CV declares the organisations
its text names (`corpus.toml`), and every posting declares its company. For one pair, a name is
*foreign* when it belongs to another corpus CV or to the posting's company and is not declared by this
pair's CV. The check flags a foreign name that appears in the tailored CV's experience section (the
whole CV when no experience heading can be found).

- **The posting's own company** in the experience section is the realistic fabrication signal: the
  model has been told about that company and nothing else, so a job "at" it is the invention most
  likely to happen.
- **Another corpus CV's employer** can only appear if something mixed prompts or responses up — the
  model never sees the other CVs. That half is a harness-integrity check more than a model check.
- **An employer the model invents from nothing is invisible to both.** A known-names list cannot catch
  a name nobody listed. `unfamiliar_phrases` lists capitalised phrases the base CV never uses, as a
  prompt for the human reader — explicitly not a check, because rewording a job title in the posting's
  vocabulary produces the same signal and is exactly what tailoring is for.
- Matching is whole-phrase, case-insensitive and whitespace-tolerant. A misspelling, an abbreviation
  nobody declared, or a name split by Markdown markup inside it will be missed.
- The experience section is found by Markdown ATX heading text (`## Experience`, `## Work History`, …).
  A model that marks sections with bold lines instead is searched as a whole CV — stricter, never
  looser.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

# Constitution §7: end-to-end tailoring under 15 seconds (PRD §8). A target, not a setting — see the
# "NOT settings, deliberately" list in `settings.py` for why a budget must not be a knob.
CONSTITUTION_BUDGET_MS: Final = 15_000

# AC-20(a): the plumbing we own (POST -> commit -> enqueue -> worker -> first successful poll) is
# asserted under 2 seconds by an API test with an instant fake LLM. AC-20(b) adds that bound to the
# model's p95, so the number lives here as the constant the verdict is computed against.
PLUMBING_BOUND_MS: Final = 2_000

# Heading words that open the part of a CV where employers are listed. Matched as substrings of the
# heading's text, case-insensitively, so "Relevant Experience" and "Employment History" both count.
_EXPERIENCE_HEADING_WORDS: Final = (
    "experience",
    "employment",
    "work history",
    "career history",
    "professional background",
)

# A Markdown ATX heading: up to three spaces, 1-6 hashes, a space, the text, optional closing hashes.
_ATX_HEADING: Final = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t#]*$")

# Two to five capitalised words on one line: the shape of an organisation, a job title or a product.
_CAPITALISED_RUN: Final = re.compile(r"[A-Z][\w&'.-]*(?:[ \t]+[A-Z][\w&'.-]*){1,4}")


def mentions(text: str, name: str) -> bool:
    """Whether `text` contains `name` as a whole phrase, ignoring case and the width of whitespace.

    Whole phrase means the characters either side are not word characters, so "Corvid" does not match
    inside "Corvidae" and "St. Aubyn" still matches across a line break between its two words.
    """
    words = name.split()
    if not words:
        return False
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words) + r"(?!\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


@dataclass(frozen=True, slots=True)
class Scope:
    """The part of a document a check searched, and whether it is the part that was asked for."""

    text: str
    located: bool


def experience_section(markdown: str) -> Scope:
    """The experience section(s) of a Markdown CV, or the whole document when none can be found.

    A section opens at an ATX heading whose text contains one of `_EXPERIENCE_HEADING_WORDS` and runs
    until the next heading of the same or a higher level, so `### Engineer — Employer` sub-headings
    stay inside it. Every matching section is collected. Falling back to the whole document when no
    heading is found makes the check stricter, never looser.
    """
    collected: list[str] = []
    open_level: int | None = None
    located = False
    for line in markdown.splitlines():
        heading = _ATX_HEADING.match(line)
        if heading is not None:
            level = len(heading.group(1))
            if open_level is not None and level <= open_level:
                open_level = None
            title = heading.group(2).casefold()
            if open_level is None and any(word in title for word in _EXPERIENCE_HEADING_WORDS):
                open_level = level
                located = True
                continue
        if open_level is not None:
            collected.append(line)
    if not located:
        return Scope(text=markdown, located=False)
    return Scope(text="\n".join(collected), located=True)


def foreign_organisations(
    document: str, *, candidates: Iterable[str], allowed: Iterable[str]
) -> tuple[str, ...]:
    """The names in `candidates` that `document` mentions and `allowed` does not contain, sorted."""
    permitted = {name.casefold() for name in allowed}
    found = {
        name for name in candidates if name.casefold() not in permitted and mentions(document, name)
    }
    return tuple(sorted(found, key=str.casefold))


def unfamiliar_phrases(document: str, base_text: str, *, limit: int = 20) -> tuple[str, ...]:
    """Capitalised multi-word phrases in `document` that `base_text` never uses, first `limit` only.

    **For the human reader, not a check.** It is noisy on purpose-built grounds: a job title reworded
    in the posting's vocabulary shows up here exactly like an invented employer does, and only a person
    can tell which is tailoring and which is fabrication. It exists because the known-names check
    above is blind to a name nobody listed.
    """
    found: dict[str, None] = {}
    for match in _CAPITALISED_RUN.finditer(document):
        phrase = match.group(0).strip(".-'")
        if len(phrase.split()) < 2 or mentions(base_text, phrase):
            continue
        found.setdefault(phrase, None)
        if len(found) >= limit:
            break
    return tuple(found)


def nearest_rank(values: Sequence[int], percentile: float) -> int:
    """The nearest-rank percentile: the smallest value with at least `percentile`% of values at or
    below it.

    Chosen over interpolation because it always returns a value that was actually observed, which is
    the honest reading of ten samples. The consequence is worth knowing when reading the report: with
    fewer than 20 samples, **p95 is the slowest run**.
    """
    if not values:
        raise ValueError("a percentile of no values is undefined")
    if not 0 < percentile <= 100:
        raise ValueError(f"percentile must be in (0, 100], got {percentile}")
    ordered = sorted(values)
    rank = math.ceil(percentile / 100 * len(ordered))
    return ordered[rank - 1]


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """AC-20(b)'s arithmetic: the model's p95 plus the plumbing bound, against the 15-second budget.

    Over budget means strictly greater, matching the spec's wording ("if `p95 + plumbing > 15 s`").
    """

    p95_ms: int
    sample_size: int
    plumbing_ms: int = PLUMBING_BOUND_MS
    budget_ms: int = CONSTITUTION_BUDGET_MS

    @property
    def total_ms(self) -> int:
        return self.p95_ms + self.plumbing_ms

    @property
    def within(self) -> bool:
        return self.total_ms <= self.budget_ms

    @property
    def margin_ms(self) -> int:
        """Headroom when positive; how far over when negative."""
        return self.budget_ms - self.total_ms
