"""ASGI middleware that rejects an oversized body before FastAPI ever touches it.

**Why this exists.** Declaring `file: Annotated[UploadFile, File(...)]` on a route hands multipart
parsing to the framework *before your handler body runs*: FastAPI resolves that parameter by calling
`await request.form()` during dependency resolution, and Starlette's `MultiPartParser` drains the
entire request stream into a `SpooledTemporaryFile` while doing it. By the time a handler's own code
— including `routers/intake.py::_read_capped`'s chunked-read loop — starts executing, the whole body
has already arrived and been spooled to disk. A cap enforced only inside the handler therefore bounds
nothing about the network transfer; it can only bound how much of an *already-received* body a
function holds in memory at once. Verified empirically against this app: an 11 MB upload against a
10 MB cap still transferred all 11,000,202 bytes before the handler's 413 was returned.

The fix has to run **before routing**, at the layer that can decline to read the body at all — a pure
ASGI middleware, wrapping every request ahead of FastAPI's own parameter resolution. Rejecting here,
by `Content-Length`, before calling `receive()`, means this app never asks the ASGI server for the
body: a compliant client sending `Expect: 100-continue` (curl and most HTTP clients do, once a body
crosses their own threshold) is still waiting for permission to send it, gets this 413 instead, and
never transmits the payload at all. That is the actual protection `_read_capped`'s docstring used to
claim for itself.

This is one of three layers, each bounding a different failure, none of them redundant with the
others (see `routers/intake.py::_read_capped`'s docstring for the other two):

1. **This middleware** — rejects an honest client (one that sends `Content-Length`) before the body
   crosses the wire, for every route it is mounted in front of.
2. **nginx's `client_max_body_size`** — bounds a client that lies about `Content-Length` or omits it;
   nginx buffers to disk, not memory, and drops the connection past the cap regardless of what this
   app does.
3. **`_read_capped`** — bounds memory *within a handler that already has a body* (chunked
   transfer-encoding carries no `Content-Length`, so layer 1 cannot see it coming).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


class MaxBodySizeMiddleware:
    """Reject a request whose declared `Content-Length` exceeds `max_bytes` with a 413, before any
    downstream ASGI app — including FastAPI's own routing and parameter resolution — is invoked.

    Deliberately pure ASGI rather than `BaseHTTPMiddleware`: the latter still constructs a
    `StreamingRequest` wrapper around `receive` and is designed to let downstream code stream the
    body through it, which is the opposite of what this middleware needs to guarantee. Never calling
    `receive()` on the rejection path is the entire mechanism — it is what leaves an
    `Expect: 100-continue` client still waiting, so this app never receives (and never pays to
    transport) a single byte of a body it has already decided to refuse.

    `exempt_prefixes` exists so a cheap, bodyless route (`/health/live`, `/health/ready`) never pays
    for a `Content-Length` check it can never fail — polled far more often than any upload endpoint.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        exempt_prefixes: tuple[str, ...] = ("/health",),
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.exempt_prefixes = exempt_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._is_exempt(scope):
            await self.app(scope, receive, send)
            return

        content_length = self._content_length(scope)
        if content_length is not None and content_length > self.max_bytes:
            await self._reject(send)
            return

        await self.app(scope, receive, send)

    def _is_exempt(self, scope: Scope) -> bool:
        path = scope.get("path", "")
        return any(path.startswith(prefix) for prefix in self.exempt_prefixes)

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        for key, value in scope.get("headers", ()):
            if key == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    # An unparsable Content-Length is not this middleware's failure to diagnose —
                    # let the request through and let the framework's own body handling reject it.
                    return None
        return None

    async def _reject(self, send: Send) -> None:
        # Same envelope the router uses for the identical `file_too_large` condition
        # (`routers/intake.py`) — a client branches on `code`, and must never see two shapes for one
        # failure depending on which layer happened to catch it.
        body = json.dumps(
            {
                "error": {
                    "code": "file_too_large",
                    "message": f"The file exceeds the {self.max_bytes}-byte limit.",
                }
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
