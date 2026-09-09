"""Table-driven tests for `sniff_cv_content_type` (T29, written **after**): the whole committed CV
fixture corpus, plus the three cases the corpus does not carry — a bare ZIP, an XLSX-shaped ZIP, and
an RTF sample (technical-plan.md's Adapters row; F-4, F-5, F-13, AC-3, AC-4).

Sniffing decides purely from bytes; whether the sniffed type can later be *read* is
`PypdfDocxTextExtractor`'s question (`test_extraction.py`), not this one — which is why
`scanned.pdf`, `encrypted.pdf` and `corrupt.pdf` are all still expected to sniff as PDF here even
though extraction fails on every one of them (AC-5).
"""

from __future__ import annotations

import io
import random
import zipfile
from pathlib import Path

import pytest

from tailorcraft.domain.intake.value_objects import CvContentType
from tailorcraft.infrastructure.intake.sniffing import _is_docx, sniff_cv_content_type

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "cvs"


def _read_fixture(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def _zip_bytes(names: list[str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, b"placeholder")
    return buffer.getvalue()


_FIXTURE_CASES = [
    pytest.param("sample.pdf", CvContentType.PDF, id="sample.pdf"),
    pytest.param("sample.docx", CvContentType.DOCX, id="sample.docx"),
    pytest.param("sample.txt", CvContentType.TXT, id="sample.txt"),
    pytest.param("scanned.pdf", CvContentType.PDF, id="scanned.pdf-magic-bytes-only"),
    pytest.param("encrypted.pdf", CvContentType.PDF, id="encrypted.pdf-magic-bytes-only"),
    pytest.param("corrupt.pdf", CvContentType.PDF, id="corrupt.pdf-magic-bytes-only"),
    pytest.param("not-a-pdf.pdf", None, id="not-a-pdf.pdf-is-actually-a-png"),
    pytest.param("tiny.txt", CvContentType.TXT, id="tiny.txt"),
]


@pytest.mark.parametrize(("fixture_name", "expected"), _FIXTURE_CASES)
def test_sniffs_the_committed_corpus(fixture_name: str, expected: CvContentType | None) -> None:
    data = _read_fixture(fixture_name)
    assert sniff_cv_content_type(data) is expected


def test_rejects_an_xlsx_shaped_zip() -> None:
    """A magic-byte check alone would accept this: an XLSX shares DOCX's four leading ZIP bytes
    (`PK\\x03\\x04`). The namelist check (`sniffing.py::_is_docx`) is what tells the two apart, and
    this is the row that proves it is load-bearing rather than decorative (F-4, AC-3)."""
    data = _zip_bytes(["xl/workbook.xml", "[Content_Types].xml"])
    assert sniff_cv_content_type(data) is None


def test_rejects_a_bare_zip_with_no_office_namelist() -> None:
    data = _zip_bytes(["readme.txt"])
    assert sniff_cv_content_type(data) is None


def test_rejects_rtf_even_though_its_body_would_otherwise_decode_as_plain_text() -> None:
    """F-4 lists RTF among the formats a 415 must reject. A plain-ASCII RTF body
    (`{\\rtf1\\ansi\\deff0 ...}`) decodes cleanly as UTF-8 and would otherwise fall straight through
    to the TXT branch — which is exactly why `sniffing.py` checks `_RTF_MAGIC` before the text check,
    per that constant's own docstring explaining why F-4 wins over AC-3's blanket text rule here."""
    data = b"{\\rtf1\\ansi\\deff0 Some CV content that is plain ASCII and would otherwise decode fine.}"
    assert sniff_cv_content_type(data) is None


def test_a_png_renamed_with_a_pdf_extension_is_rejected_by_bytes_alone() -> None:
    """AC-4: sniffing must win regardless of what the filename or a declared `Content-Type` claims.
    This function never sees either — only bytes — so this is really the same assertion as
    `not-a-pdf.pdf` above, stated once more explicitly against AC-4's wording.

    The PNG signature plus a real `IHDR` chunk header (length + type), not just the 8-byte magic:
    the magic bytes alone (`\\x89PNG\\r\\n\\x1a\\n`) contain no NUL and happen to decode cleanly under
    cp1252, which would let them fall through to the TXT branch for the wrong reason. A real PNG's
    next bytes (`\\x00\\x00\\x00\\rIHDR`) carry the NUL `_looks_like_text` is checking for, which is
    what actually makes this representative of a real image file rather than an accidental pass."""
    png_header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    assert sniff_cv_content_type(png_header + b"width/height/bit-depth bytes follow") is None


# -----------------------------------------------------------------------------------------------
# `_is_docx`'s `except Exception` (api-dev's second CRITICAL finding): a corrupted zip central
# directory can raise `struct.error`, `ValueError`, `EOFError`, `OverflowError` or
# `zipfile.LargeZipFile`, none of which is `zipfile.BadZipFile` — every one of those used to escape
# `sniff_cv_content_type` entirely and become a 500 on the upload route.
# -----------------------------------------------------------------------------------------------


def _corrupt_tail(data: bytes, seed: int, n: int, tail: int) -> bytes:
    """Flip `n` random bytes within the last `tail` bytes of `data` under a fixed `seed`.

    Corrupting anywhere in a zip rarely lands on the central directory or the End Of Central
    Directory record — most of a DOCX is the compressed part payloads, and a byte flip there just
    produces `BadZipFile` (a bad CRC or a broken deflate stream), the case already handled before
    this fix. Concentrating the corruption in the tail — where the central directory and EOCD record
    actually live — is what reliably reaches the `struct.unpack` calls that raise the *other*
    exception types this catch-all exists for (verified empirically: this exact profile reproduced
    17/300 non-`BadZipFile` escapes — `NotImplementedError` and `UnicodeDecodeError` — against the
    unfixed function, none of them the previously-caught `BadZipFile`).
    """
    rng = random.Random(seed)  # noqa: S311 — deterministic corruption, not cryptography
    mutable = bytearray(data)
    start = max(0, len(mutable) - tail)
    for position in rng.sample(range(start, len(mutable)), min(n, len(mutable) - start)):
        mutable[position] = rng.randrange(256)
    return bytes(mutable)


_TAIL_SWEEP_SEED_BASE = 42
_TAIL_SWEEP_COUNT = 300
_TAIL_SWEEP_BYTES_FLIPPED = 10
_TAIL_SWEEP_WINDOW = 300


def test_is_docx_corruption_sweep_never_raises_for_any_byte_sequence() -> None:
    """The regression test for the class of bug, not one instance of it (CLAUDE.md): 300
    independently-corrupted variants of `sample.docx`'s tail, run through `_is_docx`, must each
    return a plain `bool` — never raise. Before the `except Exception` widening, this exact sweep
    raised `NotImplementedError` or `UnicodeDecodeError` (verified empirically); a single
    hand-picked malformed zip would only ever have proven one of those two safe.
    """
    data = _read_fixture("sample.docx")
    unexpected: list[tuple[int, str, str]] = []

    for i in range(_TAIL_SWEEP_COUNT):
        variant = _corrupt_tail(
            data,
            seed=_TAIL_SWEEP_SEED_BASE + i,
            n=_TAIL_SWEEP_BYTES_FLIPPED,
            tail=_TAIL_SWEEP_WINDOW,
        )
        try:
            result = _is_docx(variant)
        except Exception as exc:
            unexpected.append((i, type(exc).__name__, str(exc)[:120]))
        else:
            assert isinstance(result, bool)

    assert not unexpected, (
        f"{len(unexpected)}/{_TAIL_SWEEP_COUNT} corrupted variants raised out of _is_docx instead "
        f"of returning False: {unexpected[:5]}"
    )
