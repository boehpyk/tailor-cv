"""`FileRef` — the storage-key grammar and its derivation from a `BaseCvId` (ADR-0011).

Pure domain tests: no filesystem, no `LocalFileStore`. `FileRef` is a validated string with a
grammar; the path only exists inside the adapter. Every expected key here is computed by hand from
a literal UUID written into the test — never by calling the code and recording what it returns,
which would give the test no source of truth independent of the implementation it is meant to
guard.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from tailorcraft.domain.intake.errors import InvalidFileRef
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType
from tailorcraft.domain.shared.files import FileRef

# A literal, well-formed key for a PDF stored under the id used throughout this module.
# ADR-0011's own example (`01/92/0192f0a1-....pdf`) is built the same way: the first two shard
# levels are the id's own leading hex digits, and the file component is the id's canonical
# (dashed) string form.
_WELL_FORMED_PDF_KEY = "01/92/0192f0a1-89ab-7cde-8123-456789abcdef.pdf"


def test_accepts_a_well_formed_key() -> None:
    """The grammar's happy path: two hex shard levels, a canonical UUID, a known extension."""
    ref = FileRef(key=_WELL_FORMED_PDF_KEY)

    assert ref.key == _WELL_FORMED_PDF_KEY


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        pytest.param(
            "../01/92/0192f0a1-89ab-7cde-8123-456789abcdef.pdf",
            "a directory traversal segment",
            id="parent-traversal",
        ),
        pytest.param(
            "/01/92/0192f0a1-89ab-7cde-8123-456789abcdef.pdf",
            "a leading slash",
            id="leading-slash",
        ),
        pytest.param(
            "01\\92\\0192f0a1-89ab-7cde-8123-456789abcdef.pdf",
            "a backslash",
            id="backslash",
        ),
        pytest.param(
            "01/92/0192f0a1-89ab-7cde-8123-456789abcdef.pdf\x00",
            "an embedded NUL",
            id="nul-byte",
        ),
        pytest.param(
            "01/92/0192f0a1-89ab-7cde-8123-456789abcdef.exe",
            "an extension outside pdf/docx/txt",
            id="bad-extension",
        ),
        pytest.param(
            "01/92/0192f0a1-89ab-7cde-8123-456789abcdef",
            "no extension at all",
            id="missing-extension",
        ),
    ],
)
def test_rejects_a_key_outside_the_storage_grammar(key: str, reason: str) -> None:
    """ADR-0011 makes a traversing or malformed key unrepresentable at the type level.

    `reason` documents *why* each case is here even though the assertion cannot use it — a reader
    scanning red output for `InvalidFileRef` should not have to reverse-engineer six one-line ids.
    """
    with pytest.raises(InvalidFileRef):
        FileRef(key=key)


def test_for_base_cv_matches_a_hand_computed_key() -> None:
    """`for_base_cv` builds `<hex[0:2]>/<hex[2:4]>/<canonical-uuid>.<ext>` from the id's own hex
    digits (ADR-0011 §1). The expected string below is computed by hand from the literal UUID, not
    by calling `for_base_cv` and recording the answer — otherwise this test could never disagree
    with the code it is meant to guard.

    UUID: 0192f0a1-89ab-7cde-8123-456789abcdef
    hex (no dashes): 0192f0a189ab7cde8123456789abcdef
      hex[0:2] = "01", hex[2:4] = "92"
    -> "01/92/0192f0a1-89ab-7cde-8123-456789abcdef.pdf"
    """
    cv_id = BaseCvId(value=UUID("0192f0a1-89ab-7cde-8123-456789abcdef"))

    ref = FileRef.for_base_cv(cv_id, CvContentType.PDF)

    assert ref.key == "01/92/0192f0a1-89ab-7cde-8123-456789abcdef.pdf"


def test_for_base_cv_is_deterministic() -> None:
    """Same id, same content type, same key — every time.

    This is what makes an internal retry of the same `BaseCvId` idempotent (F-22): the retried
    write lands on the identical storage key rather than creating a duplicate.
    """
    cv_id = BaseCvId(value=UUID("0192f0a1-89ab-7cde-8123-456789abcdef"))

    first = FileRef.for_base_cv(cv_id, CvContentType.DOCX)
    second = FileRef.for_base_cv(cv_id, CvContentType.DOCX)

    assert first == second


@pytest.mark.parametrize(
    ("content_type", "expected_extension"),
    [
        pytest.param(CvContentType.PDF, "pdf", id="pdf"),
        pytest.param(CvContentType.DOCX, "docx", id="docx"),
        pytest.param(CvContentType.TXT, "txt", id="txt"),
    ],
)
def test_file_extension_matches_the_accepted_format(
    content_type: CvContentType, expected_extension: str
) -> None:
    """The extension `FileRef.for_base_cv` puts on a key — never re-derived from the filename the
    browser sent (ADR-0011 §2)."""
    assert content_type.file_extension == expected_extension
