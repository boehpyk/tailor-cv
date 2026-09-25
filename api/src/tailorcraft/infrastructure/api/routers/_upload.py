"""The upload boundary, shared by every route that accepts a CV file (slice 2.2, T17).

1.1 built this inside `routers/intake.py` for `POST /api/base-cvs`. Slice 2.2 adds a second route that
takes the same file under the same rules — `POST /api/me/base-cvs`, a registered user's saved CV — and
the technical plan (§3) is explicit that the helpers **move here rather than being copied**: two
copies of a validation boundary drift, and the one that drifts is the one nobody is looking at.

What lives here is the part of the boundary that does not depend on *who* owns the upload:

* `read_validated_upload` — filename → capped read → empty check → the sniff in a worker thread. The
  four checks every CV upload passes in this order, each with 1.1's status and `code`.
* `commit_or_503` — the in-handler commit (FastAPI 0.141 runs a dependency's teardown after the
  response is sent, so only a commit made *inside* the handler can still turn into a 503).
* `failure_message` — the server-owned sentence for each `ExtractionFailureReason`.

What does **not** live here: resolving the owner and the rate limit. Those differ per route (a guest
session and the `session` scope for one, a user and the `user` scope for the other) and are each
route's own business. The leading underscore on the module name says the same thing as on a
function: this is `routers/`' own plumbing, not an API for anything else to import.

A refactor only (R-6): every status, `code`, message and order below is 1.1's, byte for byte.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import UploadFile, status
from fastapi.exceptions import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.intake.value_objects import (
    CvContentType,
    ExtractionFailureReason,
    OriginalFilename,
)
from tailorcraft.domain.shared.errors import DomainError
from tailorcraft.infrastructure.api.errors import domain_error_to_http_exception
from tailorcraft.infrastructure.intake.sniffing import sniff_cv_content_type
from tailorcraft.infrastructure.settings import Settings

# Read the already-received upload in fixed-size chunks and count as we go, rather than joining it
# into one second copy first and measuring that — a memory bound within the handler, not a network
# one. `MaxBodySizeMiddleware` (infrastructure/api/middleware.py) is what aborts a streaming request
# before it lands; see `read_capped`'s own docstring for why the two are not the same guarantee.
UPLOAD_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    """An upload that passed the boundary: a sanitized display name, the bytes, and the type its
    own bytes declare. Nothing in it came from the client's `Content-Type` header."""

    original_filename: OriginalFilename
    content: bytes
    content_type: CvContentType


async def read_validated_upload(file: UploadFile, settings: Settings) -> ValidatedUpload:
    """Run 1.1's upload boundary, in 1.1's order, raising its `HTTPException`s.

    Call it **after** the route's rate limit: the limit gates the expensive part of the request
    (reading, sniffing, extracting), so it is checked before any of that work starts (F-24).

    1. **Filename** (AC-7): `OriginalFilename.__post_init__` reduces to a basename and rejects
       anything that cannot be a display label — never joined to a path anywhere. 422
       `invalid_filename`, through the domain error mapping.
    2. **Size**: `read_capped` — 413 `file_too_large`.
    3. **Empty**: 422 `empty_file`.
    4. **Type**, sniffed from the bytes — never the client's Content-Type header, never the filename
       extension (F-4/F-5/F-6/AC-3/AC-4). 415 `unsupported_format`.

       In a worker thread, for the same reason `LocalFileStore` and `PypdfDocxTextExtractor` use one
       (Constitution §1, ADR-0009): `sniff_cv_content_type` is synchronous and CPU-bound, and its cost
       is chosen by the *uploader*. Opening a zip reads its whole central directory, so an archive of
       100,000 tiny entries — 8.6 MB, comfortably inside the 10 MB cap — measured 340 ms in the
       container, and 120,000 entries 410 ms. On the event loop that is a stall for every concurrent
       user of every endpoint, and it presents as "the app is slow" rather than as an error, which is
       why §1 grades it CRITICAL rather than as a style note.

       The route's limiter already ran, so the damage is bounded rather than unbounded. That is a
       cost ceiling, not a licence: a rate limit bounds how *often* the loop is stalled, never
       whether it is stalled.
    """
    try:
        original_filename = OriginalFilename(file.filename or "")
    except DomainError as exc:
        raise domain_error_to_http_exception(exc) from exc

    # The streaming abort (F-3/AC-2) already happened, if it was going to: `MaxBodySizeMiddleware`
    # ran ahead of routing and would have answered 413 before the handler was even invoked. This is
    # the in-memory-bound read over a body FastAPI has already received.
    data = await read_capped(file, settings.max_upload_bytes)

    if not data:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "empty_file", "message": "The uploaded file is empty."},
        )

    content_type = await asyncio.to_thread(sniff_cv_content_type, data)
    if content_type is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "code": "unsupported_format",
                "message": "Unsupported file format. Please upload a PDF, DOCX or TXT file.",
            },
        )

    return ValidatedUpload(
        original_filename=original_filename, content=data, content_type=content_type
    )


async def read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Read `file` back from FastAPI's already-parsed `UploadFile` in `UPLOAD_CHUNK_BYTES` chunks,
    aborting as soon as the running total exceeds `max_bytes`, rather than joining the whole thing
    into one `bytes` object first and measuring it afterward.

    **This does not bound what crosses the network** — despite what an earlier version of this
    docstring claimed. Declaring `file: Annotated[UploadFile, File(...)]` on a handler hands
    multipart parsing to the framework *before this function, or even the handler body, starts
    running*: FastAPI resolves that parameter by awaiting `request.form()` during dependency
    resolution, and Starlette's `MultiPartParser` drains the entire request body into a
    `SpooledTemporaryFile` while doing it. By the time this loop's first `file.read()` returns, the
    whole upload has already been received and spooled — this function is reading bytes back off
    disk/memory, not bytes still arriving on the wire. Verified empirically: an 11 MB upload against
    a 10 MB cap transferred all 11,000,202 bytes before this loop's 413 fired.

    So what actually guarantees what:

    * `MaxBodySizeMiddleware` (`infrastructure/api/middleware.py`), reading `Content-Length` before
      FastAPI ever calls `receive()`, is what rejects an honest client's request while it is still
      streaming — the guarantee this docstring used to (wrongly) claim for this function.
    * nginx's `client_max_body_size` bounds a client that lies about `Content-Length` or omits it
      outright, upstream of this process entirely.
    * **This function's actual job** is narrower: bound how much of an *already-received* body the
      handler holds in memory at once, rather than materializing a second full copy via
      `await file.read()` with no size argument. It also remains the only line of defense against a
      chunked-transfer-encoding request, which carries no `Content-Length` for the middleware to see
      coming (F-3/AC-2's "aborted while streaming" is achieved by the middleware for the common case;
      this loop is the fallback for the case the middleware structurally cannot catch).
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                detail={
                    "code": "file_too_large",
                    "message": f"The file exceeds the {max_bytes}-byte limit.",
                },
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def commit_or_503(db: AsyncSession) -> None:
    """Commit the request's unit of work **here**, inside the handler, or answer 503
    `service_unavailable`.

    This is not belt-and-braces — it is the only place a failed commit can still change the answer.
    FastAPI runs the exit half of a yield-dependency AFTER the response has been sent (documented
    behaviour since 0.106, measured on 0.141). So `get_session`'s post-yield `commit()` fires with
    the 201 already on the wire: if it raises, Starlette finds `response_started == True` and the
    client keeps the 201 it was already given. F-15 — "the commit fails after the file was written,
    the API answers 503" — is unreachable from there, no matter what exception handler is installed.

    Committing inside the handler's own error boundary puts the failure back where it can be
    translated. The teardown commit still runs and is then a no-op on an already-committed session,
    so nothing about the unit-of-work boundary changes for any other route.

    On failure the row is gone; the file the use case already wrote is not. That orphan is the
    deliberate survivor of this crash window (ADR-0006 §2) — a directory sweep can reclaim it,
    whereas the alternative ordering leaves a row pointing at bytes that never arrived.
    """
    try:
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "service_unavailable",
                "message": "Could not save your CV just now. Please try again.",
            },
        ) from exc


def failure_message(reason: ExtractionFailureReason, settings: Settings) -> str:
    """One user-facing sentence per `ExtractionFailureReason` — server-owned, so the client never
    re-implements this mapping (technical-plan.md's API contract).

    `NO_TEXT_LAYER`'s message names OCR as **absent, not as coming** (F-9): feature-spec.md's
    non-goals are explicit that OCR is "not a feature request answered here", so a message that reads
    as a promise would be a claim this product does not intend to keep.
    """
    if reason is ExtractionFailureReason.ENCRYPTED:
        return (
            "This PDF is password-protected, so we could not read it. "
            "Remove the password and upload it again."
        )
    if reason is ExtractionFailureReason.CORRUPT:
        return (
            "This file looks damaged or incomplete and could not be read. "
            "Try re-exporting it and uploading it again."
        )
    if reason is ExtractionFailureReason.NO_TEXT_LAYER:
        return (
            "We saved your file, but couldn't find any text in it — it looks like a scan. "
            "We don't support OCR, so try a text-based PDF, or paste your CV as a .txt file instead."
        )
    if reason is ExtractionFailureReason.TOO_SHORT:
        return (
            "We could only read a small amount of text from this file — not enough to work with. "
            "Make sure the file contains your full CV."
        )
    if reason is ExtractionFailureReason.TOO_MANY_PAGES:
        return (
            f"This PDF has more than {settings.max_cv_pages} pages, which is more than we can "
            "process. Try a shorter version of your CV."
        )
    # ExtractionFailureReason.EXTRACTOR_ERROR — the catch-all, reached two ways: the 10 s timeout
    # (F-12) and, since `PypdfDocxTextExtractor` grew its catch-all, any library failure we have no
    # better name for (a `KeyError` out of `pypdf` on a mangled cross-reference table, say). The
    # earlier wording here — "we couldn't read this file in time" — described only the first of
    # those, and would have been a confidently wrong diagnosis for the second, which is now the
    # commoner one. It names both causes rather than retreating into "something went wrong":
    # "too long" and "gave up part-way" are different things a user can act on differently, and
    # "part-way" is also what distinguishes this sentence from CORRUPT's "looks damaged" — that one
    # is a verdict on the file, this one is an admission about us.
    return (
        "We couldn't read this file — it either took too long or our reader gave up part-way "
        "through. Try again, or use a different PDF, DOCX or TXT file."
    )
