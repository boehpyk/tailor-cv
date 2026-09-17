"""Adapters for the `export` bounded context: the rendering pipeline (ADR-0017).

One parse, one normalization, three walkers. The stored Markdown is parsed once by `tokens.py`,
normalized once into the closed document grammar, and then walked by `plain_text.py`, `docx.py` and
`html.py` — the last of which is the only path that reaches `pdf.py` and WeasyPrint. `md` is not a
render at all: the stored text *is* the export.

Three vendor packages live under this package and nowhere else, one module each — `markdown_it`
(`tokens.py`), `nh3` (`html.py`), `weasyprint` (`pdf.py`) — with `docx` (`docx.py`) the fourth.
All four are on both import-linter forbidden lists, so none of them can appear outside
`infrastructure/export/` without breaking a gate.
"""

from __future__ import annotations
