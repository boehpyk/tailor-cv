"""The tailoring prompt's plumbing (slice tailoring-preserve-cv-formatting, AC-11/AC-12).

Test-after (T8). These pin what a string can prove: the CV reaches the prompt with its lines intact,
the signature still admits nothing but two strings, and the version was bumped. Whether the model
keeps the structure is `make eval`'s question, not pytest's.
"""

from __future__ import annotations

import inspect

from tailorcraft.infrastructure.llm.prompt import (
    _CV_BEGIN,
    _CV_END,
    PROMPT_VERSION,
    build_tailoring_prompt,
)

_CV = """\
# Tomasz Nowak

**Registered Nurse**
Kraków, Poland
tomasz@example.com

## Experience

**Staff Nurse**
University Hospital
2019 - 2024

- Ran the night shift's medication round
- Trained four new starters"""


def test_a_multi_line_markdown_cv_sits_verbatim_between_the_cv_markers() -> None:
    prompt = build_tailoring_prompt(_CV, "A posting.")

    between = prompt.split(_CV_BEGIN, 1)[1].split(_CV_END, 1)[0]
    assert between == f"\n{_CV}\n"


def test_the_signature_is_still_two_strings() -> None:
    params = inspect.signature(build_tailoring_prompt).parameters

    assert list(params) == ["cv_text", "posting_text"]
    assert all(p.annotation in (str, "str") for p in params.values())


def test_the_prompt_version_is_5() -> None:
    assert PROMPT_VERSION == "5"
