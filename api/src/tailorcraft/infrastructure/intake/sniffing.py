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
# `{\rtf` at offset 0: RTF markup (`{\rtf1\ansi\deff0 ...}`) is ASCII/CP1252-clean control words, so
# it decodes as plain text and would otherwise fall straight through to the TXT branch below — this
# check exists specifically to intercept it first.
#
# DECISION (made here, on purpose — T26's brief flags this as a real contradiction in the spec, not
# an oversight to quietly resolve): feature-spec.md's failure contract (F-4) lists RTF among the
# formats a 415 must reject, but its AC-3 defines TXT as "decodes as UTF-8 or cp1252 with no NUL" —
# a definition RTF satisfies. F-4 wins: handing the model a CV made of `\rtf1\ansi\deff0` control
# words is a worse outcome for the user than an honest 415 naming the three formats that work. This
# narrows AC-3's blanket text rule on purpose — a reader comparing this code to the spec should find
# this paragraph, not "fix" the disagreement back the other way.
_RTF_MAGIC = b"{\\rtf"

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
    - `{\\rtf` at offset 0 → `None` (415), **before** the text check below — see `_RTF_MAGIC`'s own
      comment for why this deliberately narrows AC-3's general text rule (F-4).
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

    if data.startswith(_RTF_MAGIC):
        return None

    if _looks_like_text(data):
        return CvContentType.TXT

    return None


def _is_docx(data: bytes) -> bool:
    """A DOCX is a zip whose namelist contains `word/document.xml`. Anything that fails to open as
    a zip at all is simply not a DOCX — this function never raises.

    "Never raises" used to be a claim resting on `zipfile` only ever signalling a bad archive with
    `BadZipFile`. It does not: parsing a deliberately malformed central directory can surface
    `struct.error`, `ValueError`, `EOFError`, `OverflowError` or `zipfile.LargeZipFile` instead,
    depending on which field the corruption lands in. Every one of those would have escaped this
    function, escaped `sniff_cv_content_type`, and become a **500 on the upload route** — for a file
    whose only crime is not being a DOCX, which is a 415. So the promise in the sentence above is now
    made structurally by the `except Exception` rather than by an allow-list of the ways a hostile
    zip was expected to be broken; that guess is exactly the one this codebase already lost once, in
    `intake/extraction.py`.

    Sniffing is a *classification* question with a boolean answer, which is what makes a blanket
    catch honest here rather than lazy: "this did not open as a DOCX" is the correct and complete
    answer to every failure mode, and there is no error information a caller could act on.
    """
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            return _DOCX_MARKER in archive.namelist()
    except Exception:
        # Nothing logged: the only thing worth saying is "not a DOCX", which is the return value,
        # and an exception message from `zipfile` can quote bytes out of the upload (Constitution §8).
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
