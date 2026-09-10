"""Adapter tests for `HttpxTrafilaturaFetcher` (`JobPostingFetcherPort`), written **after** (T30):
the real adapter against a **local stub HTTP server** on `127.0.0.1`, using a **test-module-only**
permissive `TargetAddressPolicy(allow_private=True)` — nothing under `api/src/` ever builds one
(ADR-0012, `infrastructure/posting/address_policy.py`'s own docstring).

**No test in this module makes a real outbound request.** The stub server (`_StubServer` below) is a
`http.server.ThreadingHTTPServer` bound to `127.0.0.1:0` (an OS-assigned ephemeral port), started and
stopped per test by the `stub` fixture. Every scenario — the happy path, every failure row, the
redirect chain, the byte cap, the gzip bomb, the slow trickle — is driven against this local server,
mirroring `test_extraction.py`'s "real adapter, real (local) input" approach one level up the stack.

Mirrors `test_extraction.py`'s structure: happy path, failure-contract translation, the two floor
tests (an unrecognised exception, `asyncio.CancelledError` not swallowed), and here in addition the
AC-9 wiring test and the AC-7 per-hop re-check — both specific to this adapter's SSRF obligations
(ADR-0012).
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import http.server
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from tailorcraft.domain.posting.errors import (
    JobPostingFetchFailed,
    SourceHasNoReadableText,
    SourceNotHtml,
    SourceRejectedRequest,
    SourceResponseTooLarge,
    SourceTimedOut,
    SourceTooManyRedirects,
    SourceUnreachable,
    SourceUrlNotAllowed,
)
from tailorcraft.domain.posting.value_objects import FetchFailureReason, SourceUrl
from tailorcraft.infrastructure.api.deps import get_job_posting_fetcher
from tailorcraft.infrastructure.posting.address_policy import TargetAddressPolicy
from tailorcraft.infrastructure.posting.fetching import HttpxTrafilaturaFetcher, _Extracted
from tailorcraft.infrastructure.settings import Settings

# --- The stub server -------------------------------------------------------------------------------

_RouteHandler = Callable[[http.server.BaseHTTPRequestHandler], None]


class _StubState:
    """Per-test mutable state the handler closures below read and write.

    `bytes_written` is read by the response-cap test to prove the server did not get to send the
    whole body before the client aborted — the "assert the stream was aborted early" half of AC-8 /
    P-19. A lock guards it because `ThreadingHTTPServer` runs each connection on its own thread.
    """

    def __init__(self) -> None:
        self.routes: dict[str, _RouteHandler] = {}
        self.bytes_written = 0
        self._lock = threading.Lock()

    def record_bytes(self, n: int) -> None:
        with self._lock:
            self.bytes_written += n


def _make_handler_class(state: _StubState) -> type[http.server.BaseHTTPRequestHandler]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            route = state.routes.get(self.path)
            if route is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # The client (the adapter under test) closed the connection early — exactly what the
            # response-cap and slow-trickle tests are driving it to do. Not a test failure.
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                route(self)

        def log_message(self, format: str, *args: object) -> None:
            pass  # silence — a stub server's access log is noise in `make test` output

    return _Handler


class _StubServer:
    def __init__(self) -> None:
        self.state = _StubState()
        handler_cls = _make_handler_class(self.state)
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def url(self, path: str) -> str:
        port = self._httpd.server_address[1]
        return f"http://127.0.0.1:{port}{path}"

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def stub() -> Iterator[_StubServer]:
    server = _StubServer()
    server.start()
    try:
        yield server
    finally:
        server.shutdown()


# --- Response builders used by route closures -------------------------------------------------------


def _respond(
    handler: http.server.BaseHTTPRequestHandler,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> None:
    handler.send_response(status)
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if body:
        handler.wfile.write(body)


def _redirect(
    handler: http.server.BaseHTTPRequestHandler, *, location: str, status: int = 302
) -> None:
    handler.send_response(status)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _paced_large_body(
    handler: http.server.BaseHTTPRequestHandler,
    *,
    total_bytes: int,
    chunk_size: int,
    delay_seconds: float,
    state: _StubState,
) -> None:
    """Write `total_bytes` in small, paced chunks rather than one big `write()`.

    Pacing (not buffer size) is what makes "the client aborts early" deterministic: a single large
    `write()` can be absorbed whole by the OS socket buffer on loopback regardless of what the
    client does with it, which would make the "substantially fewer bytes than the full body" assertion
    a coin flip in CI. Sleeping between small chunks means the server is still mid-stream, genuinely
    blocked on the network, at the moment the client's cap trips and closes the connection.
    """
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True
    chunk = b"a" * chunk_size
    sent = 0
    while sent < total_bytes:
        piece = chunk if sent + chunk_size <= total_bytes else chunk[: total_bytes - sent]
        handler.wfile.write(piece)
        handler.wfile.flush()
        sent += len(piece)
        state.record_bytes(len(piece))
        time.sleep(delay_seconds)


def _gzip_bomb(handler: http.server.BaseHTTPRequestHandler, *, decoded_size: int) -> None:
    """A small compressed body whose *decoded* size is `decoded_size` — the right side of the byte
    cap to measure (the module docstring on `HttpxTrafilaturaFetcher._read_capped` explains why)."""
    payload = gzip.compress(b"a" * decoded_size)
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html")
    handler.send_header("Content-Encoding", "gzip")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _slow_trickle(
    handler: http.server.BaseHTTPRequestHandler, *, iterations: int, delay_seconds: float
) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True
    for _ in range(iterations):
        handler.wfile.write(b"a")
        handler.wfile.flush()
        time.sleep(delay_seconds)


# --- Fixture content ---------------------------------------------------------------------------------

_KNOWN_TITLE = "Senior Python Engineer"
_KNOWN_PHRASE = "platform team"

_JOB_POSTING_HTML = (
    b"<html><head><title>Senior Python Engineer</title></head><body><article>"
    b"<p>We are hiring a Senior Python Engineer to join our platform team.</p>"
    b"<p>You will design services, review code, and mentor other engineers.</p>"
    b"<p>Requirements include five years of experience with Python and distributed systems.</p>"
    b"<p>We offer remote work, competitive pay, and a supportive engineering culture.</p>"
    b"</article></body></html>"
)

_JS_SHELL_HTML = (
    b"<html><head><title>Loading...</title></head>"
    b'<body><div id="root"></div><script>console.log("rendered client-side");</script></body></html>'
)


def _fetcher(
    *,
    timeout_seconds: int = 10,
    connect_timeout_seconds: float = 2.0,
    read_timeout_seconds: float = 3.0,
    max_bytes: int = 2 * 1024 * 1024,
    max_redirects: int = 3,
    extraction_timeout_seconds: int = 5,
    policy: TargetAddressPolicy | None = None,
) -> HttpxTrafilaturaFetcher:
    """A fetcher configured for the stub server, with production-shaped defaults every test can
    override the one or two settings it actually needs.

    `policy` defaults to `TargetAddressPolicy(allow_private=True)` — the **test-module-only**
    permissive policy ADR-0012 and `address_policy.py`'s docstring describe: it widens loopback and
    RFC-1918 only, never link-local, multicast, reserved or CGNAT, which is exactly what makes the
    AC-7 redirect-to-metadata test below meaningful rather than vacuous.
    """
    return HttpxTrafilaturaFetcher(
        user_agent="TailorCraftTest/1.0 (+http://example.invalid)",
        timeout_seconds=timeout_seconds,
        connect_timeout_seconds=connect_timeout_seconds,
        read_timeout_seconds=read_timeout_seconds,
        max_bytes=max_bytes,
        max_redirects=max_redirects,
        extraction_timeout_seconds=extraction_timeout_seconds,
        policy=policy if policy is not None else TargetAddressPolicy(allow_private=True),
    )


# --- Happy path: a 200 HTML page extracts text and title -------------------------------------------


async def test_a_200_html_page_is_extracted_into_text_and_title(stub: _StubServer) -> None:
    stub.state.routes["/ok"] = lambda h: _respond(
        h, headers={"Content-Type": "text/html; charset=utf-8"}, body=_JOB_POSTING_HTML
    )

    result = await _fetcher().fetch(SourceUrl(stub.url("/ok")))

    assert _KNOWN_PHRASE in result.text.value
    assert result.text.character_count >= 100
    assert result.title is not None
    assert result.title.value == _KNOWN_TITLE


# --- Redirects: inside the limit, and one over it ---------------------------------------------------


async def test_a_redirect_chain_inside_the_limit_is_followed_to_the_page(stub: _StubServer) -> None:
    stub.state.routes["/ok"] = lambda h: _respond(
        h, headers={"Content-Type": "text/html"}, body=_JOB_POSTING_HTML
    )
    stub.state.routes["/hop2"] = lambda h: _redirect(h, location=stub.url("/ok"))
    stub.state.routes["/hop1"] = lambda h: _redirect(h, location=stub.url("/hop2"))

    result = await _fetcher(max_redirects=3).fetch(SourceUrl(stub.url("/hop1")))

    assert _KNOWN_PHRASE in result.text.value


async def test_a_redirect_chain_longer_than_the_limit_is_refused(stub: _StubServer) -> None:
    """A self-redirecting loop guarantees the chain exceeds any bounded `max_redirects` — the exact
    shape a redirect loop takes in the wild, and P-17's failure row."""
    stub.state.routes["/loop"] = lambda h: _redirect(h, location=stub.url("/loop"))

    with pytest.raises(SourceTooManyRedirects):
        await _fetcher(max_redirects=3).fetch(SourceUrl(stub.url("/loop")))


# --- A 403 is a rejection, not "unreachable" --------------------------------------------------------


async def test_a_403_response_is_a_source_rejected_request(stub: _StubServer) -> None:
    stub.state.routes["/forbidden"] = lambda h: _respond(
        h,
        status=403,
        headers={"Content-Type": "text/html"},
        body=b"<html><body>Forbidden</body></html>",
    )

    with pytest.raises(SourceRejectedRequest):
        await _fetcher().fetch(SourceUrl(stub.url("/forbidden")))


# --- Content-Type gate: text/plain accepted, image/png and application/json refused (P-20) ---------


async def test_text_plain_content_type_is_accepted(stub: _StubServer) -> None:
    body = (
        "<html><body><p>"
        + "This posting is served with a plain text content type. " * 3
        + "</p></body></html>"
    ).encode()
    stub.state.routes["/plain"] = lambda h: _respond(
        h, headers={"Content-Type": "text/plain; charset=utf-8"}, body=body
    )

    result = await _fetcher().fetch(SourceUrl(stub.url("/plain")))

    assert result.text.character_count >= 100


@pytest.mark.parametrize("content_type", ["image/png", "application/json"])
async def test_a_non_html_content_type_is_refused(stub: _StubServer, content_type: str) -> None:
    stub.state.routes["/wrong-type"] = lambda h: _respond(
        h, headers={"Content-Type": content_type}, body=b"irrelevant bytes, never inspected"
    )

    with pytest.raises(SourceNotHtml):
        await _fetcher().fetch(SourceUrl(stub.url("/wrong-type")))


# --- The streamed byte cap: a large body, and a gzip bomb whose DECODED size crosses it (AC-8) -----


async def test_a_body_over_the_cap_is_refused_and_the_stream_is_aborted_early(
    stub: _StubServer,
) -> None:
    total_bytes = 5 * 1024 * 1024  # 5 MB, matching AC-8's own wording
    cap = 100_000
    stub.state.routes["/huge"] = lambda h: _paced_large_body(
        h, total_bytes=total_bytes, chunk_size=8192, delay_seconds=0.01, state=stub.state
    )

    with pytest.raises(SourceResponseTooLarge):
        await _fetcher(max_bytes=cap).fetch(SourceUrl(stub.url("/huge")))

    # The server never got to write anywhere close to the full 5 MB — the client (this adapter)
    # closed the connection once its own read total crossed the cap, well short of the end of the
    # body. This is the "aborted early" half of AC-8/P-19, checked on the side that cannot lie about
    # it: `Content-Length` was never sent for this response, so nothing here could pass by looking
    # like it obeyed a header instead of the stream.
    assert stub.state.bytes_written < total_bytes // 4


async def test_a_gzip_body_whose_decoded_size_crosses_the_cap_is_refused(stub: _StubServer) -> None:
    decoded_size = 5 * 1024 * 1024
    stub.state.routes["/bomb"] = lambda h: _gzip_bomb(h, decoded_size=decoded_size)

    with pytest.raises(SourceResponseTooLarge):
        await _fetcher(max_bytes=100_000).fetch(SourceUrl(stub.url("/bomb")))


# --- No readable text: a JS-only shell page (the fetched analogue of a scanned PDF) ------------------


async def test_a_js_only_shell_page_has_no_readable_text(stub: _StubServer) -> None:
    stub.state.routes["/shell"] = lambda h: _respond(
        h, headers={"Content-Type": "text/html"}, body=_JS_SHELL_HTML
    )

    with pytest.raises(SourceHasNoReadableText):
        await _fetcher().fetch(SourceUrl(stub.url("/shell")))


# --- A slow trickle exhausts the outer fetch budget (P-15) --------------------------------------------


async def test_a_slow_trickling_server_times_out(stub: _StubServer) -> None:
    """50 single-byte writes, 0.2 s apart — 10 s of trickle against a 1 s outer budget. Each
    individual chunk arrives well inside `read_timeout_seconds`, so it is the OUTER
    `asyncio.wait_for(self._fetch_guarded(url), timeout=self._timeout_seconds)` that trips, exactly
    the "total fetch budget expires" half of P-15 rather than a single stalled read."""
    stub.state.routes["/slow"] = lambda h: _slow_trickle(h, iterations=50, delay_seconds=0.2)

    with pytest.raises(SourceTimedOut):
        await _fetcher(timeout_seconds=1, read_timeout_seconds=0.5).fetch(
            SourceUrl(stub.url("/slow"))
        )


# --- Extraction exceeding its OWN budget is a separate timeout from the outer fetch budget (P-23) --


async def test_extraction_exceeding_its_own_timeout_budget_is_source_timed_out(
    stub: _StubServer,
) -> None:
    """P-23: "Extraction exceeds its 5 s budget -> 504 `source_timed_out`, logged with
    `source_host`, `duration_ms`, `outcome=timed_out`."

    A DIFFERENT code path from `test_a_slow_trickling_server_times_out` above: that one trips the
    OUTER `asyncio.wait_for(self._fetch_guarded(url), timeout=self._timeout_seconds)` wrapping the
    whole fetch, driven by a server that trickles bytes slowly. This one trips the INNER
    `asyncio.wait_for(asyncio.to_thread(self._extract_sync, html), timeout=self._extraction_timeout_seconds)`
    inside `_read_and_extract`, which has its own `except TimeoutError` -> `SourceTimedOut`
    translation — a fully-fetched, ordinary page whose *extraction* alone cannot finish in time.

    `extraction_timeout_seconds=0` is a deadline `asyncio.wait_for` cannot possibly meet regardless
    of how fast `trafilatura.extract` actually runs — the same shape `test_intake.py`'s
    `test_extraction_exceeding_the_timeout_records_extractor_error` uses for
    `CvTextExtractorPort`'s identical budget-of-zero trick, chosen for the same reason: it forces
    the timeout deterministically without a real multi-second sleep in the suite.

    This test's whole reason to exist is discriminating the branch it names: reverting
    `_read_and_extract`'s `except TimeoutError as exc: raise SourceTimedOut() from exc` to
    `raise RuntimeError(...)` was verified BY HAND to turn this test red (see the QA report for the
    verbatim failure) — a passing run alone does not prove that, which is why it is recorded here.
    """
    stub.state.routes["/ok"] = lambda h: _respond(
        h, headers={"Content-Type": "text/html"}, body=_JOB_POSTING_HTML
    )

    with pytest.raises(SourceTimedOut):
        await _fetcher(extraction_timeout_seconds=0).fetch(SourceUrl(stub.url("/ok")))


# --- A closed port is a connection refused, and must be SourceUnreachable, not the FETCHER_ERROR ---
# --- floor (P-14) -------------------------------------------------------------------------------


async def test_a_closed_port_on_localhost_is_source_unreachable() -> None:
    """P-14: "Connection refused, reset, or TLS handshake failure -> 502 `source_unreachable`,
    logged with `source_host`, `outcome=unreachable`, plus `error_type`. Tested by: adapter test
    against a closed port."

    A socket is bound to `127.0.0.1` on an OS-assigned ephemeral port and immediately closed —
    freeing the port while guaranteeing the OS actively refuses the very next connection attempt to
    it (`ECONNREFUSED`), which is the cheapest, fully-local way to drive a real connection-refused
    failure without depending on an external host or a firewall rule. No stub server is started for
    this test; the whole point is that nothing is listening.

    `SourceUnreachable`'s own docstring is explicit that "the connection was refused" is one of the
    conditions it covers, alongside DNS failure. This is the only test in this module — or anywhere
    in the suite — that drives a refused connection through the real adapter; every other exercise
    of `SourceUnreachable` goes through a fake fetcher at the use-case or API layer.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    with pytest.raises(SourceUnreachable):
        await _fetcher().fetch(SourceUrl(f"http://127.0.0.1:{port}/"))


# --- The two floor tests: an unrecognised exception, and CancelledError NOT swallowed (P-25, P-26) --


async def test_an_unrecognised_exception_becomes_fetcher_error_with_nothing_leaked(
    stub: _StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `except Exception` floor (ADR-0012 obligation 10): a library failure with no specific
    translation must still leave `fetch` as a `JobPostingFetchFailed` subclass, never a bare 500 —
    and it must do so without chaining the original exception (`from None`) or quoting its message,
    both of which could carry a fragment of the fetched page into a log line or a Sentry report."""
    stub.state.routes["/ok"] = lambda h: _respond(
        h, headers={"Content-Type": "text/html"}, body=_JOB_POSTING_HTML
    )
    secret_fragment = "Confidential internal req id: REQ-88291-do-not-share"

    def _boom(self: HttpxTrafilaturaFetcher, html: str) -> _Extracted:
        raise RuntimeError(secret_fragment)

    monkeypatch.setattr(HttpxTrafilaturaFetcher, "_extract_sync", _boom)

    with pytest.raises(JobPostingFetchFailed) as exc_info:
        await _fetcher().fetch(SourceUrl(stub.url("/ok")))

    assert exc_info.value.reason is FetchFailureReason.FETCHER_ERROR
    assert exc_info.value.__cause__ is None
    assert secret_fragment not in str(exc_info.value)


async def test_cancelled_error_during_a_fetch_is_not_swallowed(
    stub: _StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`except Exception`, never `except BaseException`, in `fetch`'s floor: a genuinely cancelled
    request must keep cancelling rather than being reported as `JobPostingFetchFailed`. Mirrors
    `test_extraction.py`'s identical pin for `CvTextExtractorPort` (1.1's version of this same rule)."""
    stub.state.routes["/ok"] = lambda h: _respond(
        h, headers={"Content-Type": "text/html"}, body=_JOB_POSTING_HTML
    )

    def _slow_extract_sync(self: HttpxTrafilaturaFetcher, html: str) -> _Extracted:
        time.sleep(0.5)
        return _Extracted(text="x" * 150, title="")

    monkeypatch.setattr(HttpxTrafilaturaFetcher, "_extract_sync", _slow_extract_sync)

    task = asyncio.ensure_future(_fetcher(timeout_seconds=10).fetch(SourceUrl(stub.url("/ok"))))
    await asyncio.sleep(0.05)  # let the thread actually start before cancelling it
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --- AC-9: production wiring builds the STRICT policy, never the permissive test-only one -----------


def test_production_wiring_builds_the_strict_address_policy(settings: Settings) -> None:
    """The whole testability seam in one assertion: `deps.get_job_posting_fetcher` must build
    `TargetAddressPolicy.strict()` — `allow_private is False` — and there must be no setting that
    weakens it. This is the test that fails the day someone "simplifies" the wiring by passing a
    permissive policy through, or adds a settings flag that does the same thing (ADR-0012, "no off
    switch")."""
    fetcher = get_job_posting_fetcher(settings)

    assert isinstance(fetcher, HttpxTrafilaturaFetcher)
    assert fetcher._policy.allow_private is False


# --- AC-7: the per-hop re-check refuses a redirect to a link-local address even under the -----------
# --- permissive test policy, which never widens link-local (address_policy.py's own docstring) -----


async def test_a_redirect_to_a_link_local_address_is_refused_even_under_the_permissive_policy(
    stub: _StubServer,
) -> None:
    """`TargetAddressPolicy(allow_private=True)` widens loopback and RFC-1918 ONLY — never
    link-local, multicast, reserved or CGNAT (`address_policy.py`'s docstring is explicit about
    this, and the reason is exactly this test). A reputable-looking host that 302s to the cloud
    metadata endpoint must be refused at the hop, under any policy this codebase can construct."""
    stub.state.routes["/redirect-metadata"] = lambda h: _redirect(
        h, location="http://169.254.169.254/latest/meta-data/"
    )

    with pytest.raises(SourceUrlNotAllowed):
        await _fetcher().fetch(SourceUrl(stub.url("/redirect-metadata")))


async def test_the_real_adapter_logs_the_host_and_nothing_else_from_the_url(
    stub: _StubServer, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-18 / Constitution §8, asserted against the **real** adapter's own logging.

    **This test exists because the one that was supposed to cover this could not.**
    `tests/api/test_posting.py::test_a_full_fetch_never_logs_the_text_or_the_urls_path_or_query`
    is named as though it proves the whole fetch logs nothing sensitive, but it runs against a fake
    `JobPostingFetcherPort` installed through `app.dependency_overrides` — so it never calls
    `HttpxTrafilaturaFetcher._log_outcome`, `_log_event`, `_log_unreachable` or
    `_log_unexpected_error` at all. Those four are the only functions in the slice that touch a
    `SourceUrl` near a log line, which makes them the entire subject of the rule. Its assertions
    could not fail if every one of them logged `url.value`.

    So this asserts both halves, and the positive half is the one that keeps it honest: a test that
    only checked for absence would pass identically against an adapter that logged **nothing**, and
    would then stay green while someone added a line — which is the regression it exists to catch.

    `caplog` rather than `structlog.testing.capture_logs()`, for the reason
    `tests/api/test_intake.py`'s module docstring sets out at length: `cache_logger_on_first_use`
    means a module-level logger bound during an earlier test keeps a reference to that test's
    processor list, which `capture_logs`'s in-place mutation cannot reach.
    """
    secret_path = "/secret-path-abc123"
    secret_query = "ref=xyz789"
    body_marker = b"Northwind-Logistics-Confidential-Marker"
    page = _JOB_POSTING_HTML.replace(b"</article>", body_marker + b"</article>")
    # Registered WITH the query string: the stub matches on `self.path`, which for a real request
    # includes it. Keying on the bare path 404s, which surfaces as `SourceRejectedRequest` and would
    # make this test pass for the wrong reason — a failed fetch logs less than a successful one.
    stub.state.routes[f"{secret_path}?{secret_query}"] = lambda h: _respond(
        h, headers={"Content-Type": "text/html; charset=utf-8"}, body=page
    )

    url = SourceUrl(f"{stub.url(secret_path)}?{secret_query}")
    with caplog.at_level(logging.INFO):
        await _fetcher().fetch(url)

    assert caplog.records, "expected the fetch to have produced at least one log record"
    output = caplog.text

    # The negative half: nothing identifying this particular posting may appear.
    assert "secret-path-abc123" not in output, "the URL's path reached a log line"
    assert secret_query not in output, "the URL's query reached a log line"
    assert "xyz789" not in output, "a query value reached a log line"
    assert body_marker.decode() not in output, "the fetched page's text reached a log line"

    # The positive half: the host MUST be there. Without this the test passes against an adapter
    # that logs nothing at all, and tells you nothing the day someone adds a line.
    assert url.host in output, "the host should be logged — it is the one part of a URL that may be"
