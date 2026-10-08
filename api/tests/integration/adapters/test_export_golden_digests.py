"""Golden digests of what `main` (`bfb98a9`, the last commit before slice 3.2) produces, pinned
**before** 3.2 moves anything (T9; AC-10, AC-14, AC-16).

Every digest below is SHA-256 of bytes the pre-3.2 pipeline produced; rendering code is unchanged
since `bfb98a9` apart from T6's signature threading (`git diff bfb98a9 --
api/src/tailorcraft/infrastructure/export/` shows only that). They are recorded so that T13 (the
renderer honouring a layout, `STYLESHEET` -> `layouts.CLASSIC_STYLESHEET`) has something
independent to disagree with.

**T13 changes exactly one line here:** the `STYLESHEET` import below becomes
`from tailorcraft.infrastructure.export.layouts import CLASSIC_STYLESHEET`. No digest changes.

Ordered before T8's RED (the task list has it after) so that this passing commit never lands on top
of a red suite.
"""

from __future__ import annotations

import hashlib

import pytest

from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.export.html import wrap_in_document
from tailorcraft.infrastructure.export.layouts import CLASSIC_STYLESHEET
from tailorcraft.infrastructure.export.renderer import MarkdownDocumentRenderer
from tailorcraft.infrastructure.settings import Settings
from tests.fixtures.documents import MARKDOWN_FIXTURE_CORPUS


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# (corpus index, md digest, txt digest) — the corpus order is `MARKDOWN_FIXTURE_CORPUS`'s.
_MD_TXT_DIGESTS = [
    (
        "61addf3b4c2579ee6f3d850618425d3d977d5ca9ee87c898cf03dc18ffd54036",
        "c2ef1a298d6fbb02061f74255990a8ac18d2950f6b0dc65eb43ab49f5fcd77c6",
    ),
    (
        "1113f414ddabdac67daa31a1656ee05e6d3de42d690c01a985725d19953ec09a",
        "20bfbee6f8fd1988fe6079daf7a86343aa31eafb5a5e3cba264260f5dbcf9c01",
    ),
    (
        "02a77ba1744f2ba1cc23052a3acd64e56c91b6dd1eb623f600fc40046a6913cf",
        "29edf4314517addcbb5838ad1c0b17956041b7d061e20232aabbc41a3e59a959",
    ),
    (
        "bbcac8e16ddfdc3f5f1cec21631ee63dd9e415e73716f78d4d3bb904ad5cfadb",
        "9bbbe10efc25914cecf4a4a1d16439dffe904899ffe5ac73a53279edfe63faf1",
    ),
    (
        "ae8421a71da6fa56e2cf06cf86a9af79898b6e5242ca9479d774ac994d3c730e",
        "b7fe454df583158c769e244953ae3ad451a619bcad8e0d0c146f66f0a9bc8b54",
    ),
    (
        "c4bac55800539059872bdb8e99d7d0e7057f304fe4370e64ea0462cb9675ffb5",
        "23f5a5fb84c1f7af481356c06f875a032079ae44b427198b3deae0c0939f48d9",
    ),
    (
        "01d0cc4c9ae2b1f266a1f59f6cea47f7444106e1a4e52c20043713acba8374a2",
        "30d7dcd837f2d82311f33ee32a90aa6d5ddc1fbc61650f309e14fc4c5f244b03",
    ),
    (
        "7c5805bd30f5a59210f20d4368eb8b9c416ac5157fc7f4648050a3d266104dc0",
        "8ed6a34dbfaadc253e592fb205abb44c7337bbca049fa76b5f3d4c43ed4dce94",
    ),
]

_CLASSIC_STYLESHEET_SHA256 = "3fa986745a7c3eea4804b367395ce2c542f2adea7d647cb1881481c9683a0175"

_SHELL_FRAGMENT = "<p>x</p>"
_SHELL_SHA256 = {
    TailoredDocumentKind.CV: "411fdc07202661c46d4539e31dc8fe5acccfcfd328a8b22ed6c6d38564161c7c",
    TailoredDocumentKind.COVER_LETTER: (
        "2e9b32396bc9a1c06257e5a689c28c4847e83d0d55d97e53dd71ddbab289e759"
    ),
}


def test_the_digest_table_covers_the_whole_corpus() -> None:
    assert len(_MD_TXT_DIGESTS) == len(MARKDOWN_FIXTURE_CORPUS)


@pytest.mark.parametrize("fmt", [ExportFormat.MD, ExportFormat.TXT], ids=["md", "txt"])
@pytest.mark.parametrize("index", range(len(_MD_TXT_DIGESTS)))
async def test_md_and_txt_output_is_byte_identical_to_main(
    settings: Settings, index: int, fmt: ExportFormat
) -> None:
    """AC-10."""
    fixture = MARKDOWN_FIXTURE_CORPUS[index]
    expected = _MD_TXT_DIGESTS[index][0 if fmt is ExportFormat.MD else 1]

    rendered = await MarkdownDocumentRenderer(settings).render(
        fixture.markdown,
        document=TailoredDocumentKind.CV,
        format=fmt,
        layout_template=None,
    )

    assert _sha(rendered) == expected, fixture.name


def test_the_classic_stylesheet_is_byte_identical_to_mains() -> None:
    """AC-14."""
    assert _sha(CLASSIC_STYLESHEET.encode()) == _CLASSIC_STYLESHEET_SHA256


@pytest.mark.parametrize("document", list(TailoredDocumentKind))
def test_the_document_shell_is_unchanged_from_main(document: TailoredDocumentKind) -> None:
    """AC-16: the shell never carries a layout."""
    html = wrap_in_document(_SHELL_FRAGMENT, document)

    assert _sha(html.encode()) == _SHELL_SHA256[document]
