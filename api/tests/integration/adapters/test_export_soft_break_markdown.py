"""AC-18: the MD download is the stored text, byte for byte, even with soft breaks in it."""

from __future__ import annotations

from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.export.renderer import MarkdownDocumentRenderer
from tailorcraft.infrastructure.settings import Settings
from tests.fixtures.documents import SOFT_BREAK_FIXTURE_MARKDOWN


async def test_md_export_of_a_soft_break_document_is_the_stored_text(settings: Settings) -> None:
    rendered = await MarkdownDocumentRenderer(settings).render(
        SOFT_BREAK_FIXTURE_MARKDOWN,
        document=TailoredDocumentKind.CV,
        format=ExportFormat.MD,
        layout_template=None,
    )

    assert rendered == SOFT_BREAK_FIXTURE_MARKDOWN.encode()
