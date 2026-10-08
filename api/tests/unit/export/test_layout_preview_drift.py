"""AC-38 (backend half): the checked-in previews cannot silently drift from the stylesheets.

`api/tests/fixtures/layout_previews.json` holds the SHA-256 of each stylesheet the previews were
rendered from. Edit a stylesheet without regenerating the previews and this goes red.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tailorcraft.domain.export.value_objects import LayoutTemplate
from tailorcraft.infrastructure.export.layouts import stylesheet_for

_FIXTURE = Path(__file__).parents[1].parent / "fixtures" / "layout_previews.json"


def test_fixture_names_exactly_the_layouts() -> None:
    assert set(json.loads(_FIXTURE.read_text())) == {m.value for m in LayoutTemplate}


@pytest.mark.parametrize("layout", list(LayoutTemplate), ids=lambda m: m.value)
def test_preview_was_rendered_from_the_current_stylesheet(layout: LayoutTemplate) -> None:
    recorded = json.loads(_FIXTURE.read_text())[layout.value]
    actual = hashlib.sha256(stylesheet_for(layout).encode()).hexdigest()
    assert actual == recorded, (
        f"the {layout.value} stylesheet changed since its preview was rendered: "
        "run make layout.previews and commit the new previews and fixture"
    )
