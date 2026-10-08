"""Dev-time only: render the synthetic model CV once per PDF layout (3.2, T21; plan §0.11).

Run by `make layout.previews` inside the api container; never part of an image or CI. Writes
`<out_dir>/<layout>.pdf` through the real `MarkdownDocumentRenderer` (refusing fetcher included) and
`<out_dir>/layout_previews.json`, mapping each layout id to the SHA-256 of its stylesheet.

It imports the fixture from `tests/` on purpose: the fixture is the single synthetic CV (no real
person), and a copy here would be a second one to drift.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixtures.documents import MODEL_CV_FIXTURE_MARKDOWN

from tailorcraft.domain.export.value_objects import ExportFormat, LayoutTemplate
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.export.layouts import stylesheet_for
from tailorcraft.infrastructure.export.renderer import MarkdownDocumentRenderer
from tailorcraft.infrastructure.settings import get_settings


async def render_all() -> dict[LayoutTemplate, bytes]:
    renderer = MarkdownDocumentRenderer(get_settings())
    return {
        layout: await renderer.render(
            MODEL_CV_FIXTURE_MARKDOWN,
            document=TailoredDocumentKind.CV,
            format=ExportFormat.PDF,
            layout_template=layout,
        )
        for layout in LayoutTemplate
    }


def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    pdfs = asyncio.run(render_all())
    for layout, pdf in pdfs.items():
        (out_dir / f"{layout.value}.pdf").write_bytes(pdf)
    manifest = {
        layout.value: hashlib.sha256(stylesheet_for(layout).encode()).hexdigest() for layout in pdfs
    }
    (out_dir / "layout_previews.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
