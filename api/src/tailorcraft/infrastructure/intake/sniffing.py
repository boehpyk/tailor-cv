"""Content sniffing for uploaded CVs: decide the real format from the bytes (Constitution §8).

The client's `Content-Type` header and the filename's extension are both attacker-controlled and
both ignored. This is the validation boundary for "what kind of file is this" — the domain and
everything downstream trust `CvContentType` completely because nothing reaches it without passing
through here first.

Hand-rolled rather than `libmagic`/`python-magic` on purpose: three formats fit in about 40 lines,
and a hand-rolled check needs no system package to keep in step between `docker/api/Dockerfile` and
CI — one fewer thing that can drift.
"""

from __future__ import annotations

import zipfile
from io import BytesIO

from tailorcraft.domain.intake.value_objects import CvContentType

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_DOCX_MARKER = "word/document.xml"

# Bounds the UTF-8/cp1252 decode-and-scan below to a fixed amount of work regardless of how large
# `data` is — sniffing must stay cheap even for a file that will later be rejected on size.
_TEXT_SNIFF_WINDOW = 8 * 1024


def sniff_cv_content_type(data: bytes) -> CvContentType | None:
    """Return the sniffed `CvContentType`, or `None` if `data` matches none of the three accepted
    formats — the caller turns `None` into a 415.

    - `%PDF-` at offset 0 → PDF.
    - `PK\\x03\\x04` **and** the zip's namelist contains `word/document.xml` → DOCX. The namelist
      check is load-bearing, not decorative: a magic-byte check alone would also accept an `.xlsx`,
      a `.pptx`, an `.odt`, or a bare zip archive, all of which share the same four leading bytes as
      every Office Open XML format.
    - Otherwise: decodes as UTF-8, falling back to cp1252, with **no NUL byte** in the first 8 KiB →
      TXT. The NUL check is what keeps an arbitrary binary blob that happens to decode from being
      accepted as "plain text".
    - Otherwise `None`.
    """
    if data.startswith(_PDF_MAGIC):
        return CvContentType.PDF

    if data.startswith(_ZIP_MAGIC):
        # Any zip — DOCX or otherwise — is refused here rather than falling through to the text
        # check below. A zip is binary; it must be *classified* by its namelist, never guessed at
        # by whether it happens to decode as text (an .xlsx, a .pptx, an .odt or a bare zip archive
        # all share these four leading bytes, and none of them is a CV).
        return CvContentType.DOCX if _is_docx(data) else None

    if _looks_like_text(data):
        return CvContentType.TXT

    return None


def _is_docx(data: bytes) -> bool:
    """A DOCX is a zip whose namelist contains `word/document.xml`. Anything that fails to open as
    a zip at all is simply not a DOCX — this function never raises."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            return _DOCX_MARKER in archive.namelist()
    except zipfile.BadZipFile:
        return False


def _looks_like_text(data: bytes) -> bool:
    window = data[:_TEXT_SNIFF_WINDOW]
    if b"\x00" in window:
        return False

    try:
        window.decode("utf-8")
        return True
    except UnicodeDecodeError:
        pass

    try:
        window.decode("cp1252")
        return True
    except UnicodeDecodeError:
        return False
