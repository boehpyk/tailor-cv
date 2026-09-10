"""AC-10 — the event-loop concurrency measurement for `HttpxTrafilaturaFetcher` (T31, `slow`).

**What this measures: event-loop LIVENESS, not throughput.** `HttpxTrafilaturaFetcher.fetch` awaits
its I/O (the guarded request, DNS through the loop's own `getaddrinfo`) and runs the one CPU-bound
step — HTML-to-text extraction — in a worker thread via `asyncio.to_thread` under `asyncio.wait_for`
(`infrastructure/posting/fetching.py`, step 12). That buys the loop the ability to keep answering
*other* requests while an extraction runs; it does **not** buy the four concurrent extractions
themselves four times the CPU. `trafilatura`'s parse is pure Python, so the GIL still serialises the
actual work across however many threads it is spread over — four concurrent extractions of a large
page can (and, per ADR-0009's addendum for the analogous `pypdf` case, do) take slightly *longer*
in wall-clock terms than four run serially, not shorter. Threads here are bought for liveness, not
speed, and this test is written to make exactly that distinction visible: it asserts the loop stayed
responsive, and it does not assert anything about how fast the four fetches themselves completed.

**Method**, deliberately the same shape as ADR-0009's addendum
(`docs/adr/0009-inline-cv-text-extraction-in-a-thread-pool.md`) so the two numbers are comparable: a
page with a non-trivial amount of markup (see `_NOISE_ITEM_COUNT` below) is fetched **four times
concurrently** from a local stub server while the trivial, no-I/O `/health/live` route is hammered
on the same event loop. `trafilatura`'s parse cost scales with the size of the DOM it has to walk,
not with how much of it survives extraction, so the page is built as a large amount of
navigation/footer boilerplate (which `favor_precision=True` discards) around one small, genuine
job-posting `<article>` — this keeps the *extracted* text safely inside `JobPostingText`'s
100-30,000-character range (a first draft of this fixture extracted to over 450,000 characters by
using the large content as the article body itself, which made every fetch fail with
`SourceTextTooLong` before the measurement could run at all). The noise count is deliberately modest
rather than matching ADR-0009's ~260 ms `pypdf` figure: four concurrently GIL-contending extraction
threads plus a hammering main thread cost far more wall-clock time than the same work in isolation,
so the fixture is sized to make the whole test finish in a few seconds rather than to reproduce
ADR-0009's exact number.

**The GIL is the constraint here, not the core count, and the distinction is worth stating because
the first version of this paragraph got it wrong.** It claimed the container ran under a one-CPU
cgroup quota; it does not — `cpu.max` reads `max 100000` (no quota at all) and
`sched_getaffinity` reports 12 CPUs. Those cores buy nothing for this measurement: `trafilatura`'s
parse is pure Python, so the GIL serialises the four extraction threads no matter how many cores are
idle beside them. That is precisely why threading this work buys **event-loop liveness rather than
throughput** (ADR-0009's addendum), and why the assertion below is about the health check's latency
and not about how fast four fetches finish. ADR-0009 found a **374 ms** stall
from a synchronous "cheap" DOCX zip-directory read done inline in a route — the health check's p50
stayed under a millisecond there specifically *because* the CPU-bound work was kept off the loop,
and this test pins the same property for the fetcher's extraction step.
"""

from __future__ import annotations

import asyncio
import http.server
import statistics
import threading
import time
from collections.abc import Iterator

import pytest
from httpx import AsyncClient

from tailorcraft.domain.posting.value_objects import SourceUrl
from tailorcraft.infrastructure.posting.address_policy import TargetAddressPolicy
from tailorcraft.infrastructure.posting.fetching import HttpxTrafilaturaFetcher

pytestmark = pytest.mark.slow

# --- A static, large job-posting page, served identically on every request -------------------------
#
# The bulk of the page is navigation/footer boilerplate that `trafilatura`'s `favor_precision=True`
# discards — parse COST scales with the size of the DOM, not with how much of it survives extraction
# — so the genuine content stays a short `<article>`, keeping the extracted text comfortably inside
# `JobPostingText`'s 100-30,000-character range. See the module docstring for why this matters: a
# large page used AS the article body extracts to hundreds of thousands of characters and every
# fetch fails with `SourceTextTooLong` before the measurement ever runs.

_NOISE_SENTENCE = (
    "Explore more listings, browse categories, and manage your account preferences here. "
)
_NOISE_ITEM_COUNT = 1_500


def _large_job_posting_html() -> bytes:
    noise = "".join(
        f"<li>{_NOISE_SENTENCE}Link item number {i} in the site navigation.</li>"
        for i in range(_NOISE_ITEM_COUNT)
    )
    article = (
        "<article>"
        "<p>We are hiring a Senior Python Engineer to join our platform team.</p>"
        "<p>You will design services, review code, and mentor other engineers.</p>"
        "<p>Requirements include five years of experience with Python and distributed systems.</p>"
        "<p>We offer remote work, competitive pay, and a supportive engineering culture.</p>"
        "</article>"
    )
    page = (
        "<html><head><title>Senior Python Engineer</title></head>"
        f"<body><nav><ul>{noise}</ul></nav>{article}<footer><ul>{noise}</ul></footer>"
        "</body></html>"
    )
    return page.encode()


_LARGE_PAGE = _large_job_posting_html()


class _LargePageHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_LARGE_PAGE)))
        self.end_headers()
        self.wfile.write(_LARGE_PAGE)

    def log_message(self, format: str, *args: object) -> None:
        pass  # silence — a stub server's access log is noise in `make test` output


class _LargePageServer:
    def __init__(self) -> None:
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LargePageHandler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def url(self) -> str:
        port = self._httpd.server_address[1]
        return f"http://127.0.0.1:{port}/large-posting"

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def large_page_server() -> Iterator[_LargePageServer]:
    server = _LargePageServer()
    server.start()
    try:
        yield server
    finally:
        server.shutdown()


def _fetcher() -> HttpxTrafilaturaFetcher:
    """The **test-module-only** permissive policy (ADR-0012) — nothing under `api/src/` builds one.
    Bounds are generous rather than tight: this test is measuring the event loop, not the fetcher's
    failure contract, so nothing here should come close to timing out or hitting the byte cap."""
    return HttpxTrafilaturaFetcher(
        user_agent="TailorCraftTest/1.0 (+http://example.invalid)",
        timeout_seconds=20,
        connect_timeout_seconds=5.0,
        read_timeout_seconds=10.0,
        max_bytes=5 * 1024 * 1024,
        max_redirects=3,
        extraction_timeout_seconds=15,
        policy=TargetAddressPolicy(allow_private=True),
    )


async def _hammer_health_live(client: AsyncClient, *, stop: asyncio.Event) -> list[float]:
    """Fire `/health/live` requests back-to-back until `stop` is set, timing each one.

    `/health/live` does no I/O at all (`routers/health.py`): it answers as soon as the event loop
    schedules its coroutine. Any latency above noise is therefore latency the loop spent doing
    something else before it got around to this request — which is exactly what a blocked loop looks
    like from the outside, and exactly what four inline (non-threaded) extractions would produce.

    **The trailing `await asyncio.sleep(...)` is load-bearing, not decoration.** `client` here is an
    in-process `httpx.ASGITransport` client, and `/health/live` performs no real I/O of any kind —
    so a request against it can resolve through a chain of nested coroutine `await`s that never hits
    a genuine OS-level suspension point (no socket, no disk, no thread hop). Without an explicit
    yield, `asyncio`'s scheduler has no opportunity to switch to a sibling task between iterations,
    and this loop's own `Task` starves every other task on the loop **forever**, including the one
    meant to stop it — found the hard way while writing this test: the exact shape below, with a bare
    `await asyncio.sleep(0)` in place of the timed sleep, hung indefinitely (confirmed with
    `faulthandler.dump_traceback_later`, which showed the main thread parked inside FastAPI's routing
    dispatch with no other task ever getting a turn — `sleep(0)` only reschedules onto the *current*
    ready-queue pass, and this loop's own next iteration was consistently ready before the sibling
    task's continuation, starving it just as completely as no yield at all). A small positive delay
    (rather than `sleep(0)`) is what actually lets the fetch tasks — and the outer test coroutine —
    get scheduled turns; it also caps the hammering rate to something sane instead of an unbounded
    tight loop, which is worth doing on its own merits — an unbounded loop makes the samples measure
    the loop rather than the fetcher.
    A real deployed `/health/live` request arrives over an actual socket and would not have this
    problem; it is specific to driving the app in-process, and the fetcher's own I/O (real sockets,
    a real worker thread) does not share this failure mode, which is exactly why gathering it
    alongside this loop works once this loop cooperates.
    """
    latencies: list[float] = []
    while not stop.is_set():
        started = time.perf_counter()
        response = await client.get("/health/live")
        latencies.append(time.perf_counter() - started)
        assert response.status_code == 200
        await asyncio.sleep(0.001)
    return latencies


async def test_health_live_p50_stays_under_5ms_during_four_concurrent_fetches(
    client: AsyncClient, large_page_server: _LargePageServer
) -> None:
    """AC-10: hammer `/health/live` while four concurrent fetches of a large page run, and assert
    the health check's p50 stays under 5 ms — the same measurement shape ADR-0009's addendum used,
    so the two numbers are comparable. See the module docstring for what this does and does not
    claim about the fetches' own speed."""
    fetchers = [_fetcher() for _ in range(4)]
    stop = asyncio.Event()

    hammer_task = asyncio.ensure_future(_hammer_health_live(client, stop=stop))
    await asyncio.sleep(0)  # let the hammering task actually start before the fetches begin

    # `stop.set()` in `finally`, not after a bare `await gather(...)`: if any fetch raises, a bare
    # sequence would skip straight past `stop.set()` and leave `_hammer_health_live`'s `while not
    # stop.is_set()` loop running forever on this session-scoped event loop — a real failure mode
    # hit while writing this test (a first draft of the fixture page made every fetch raise
    # `SourceTextTooLong`, and the resulting orphaned hammering task looked exactly like a hang).
    try:
        await asyncio.gather(
            *(fetcher.fetch(SourceUrl(large_page_server.url)) for fetcher in fetchers)
        )
    finally:
        stop.set()
    latencies = await asyncio.wait_for(hammer_task, timeout=5)

    # A sanity floor on the sample size, not a performance assertion: if the hammering loop only
    # got to run a handful of times, the p50 below would be measuring noise rather than concurrent
    # behaviour, and that failure mode should be loud rather than silently passing on 2 samples.
    assert len(latencies) >= 20, (
        f"only {len(latencies)} /health/live samples were taken during the four concurrent "
        "fetches — too few to say anything about event-loop liveness"
    )

    p50 = statistics.median(latencies)
    assert p50 < 0.005, (
        f"/health/live p50 was {p50 * 1000:.2f} ms during four concurrent fetches "
        f"(n={len(latencies)}, max={max(latencies) * 1000:.2f} ms) — the event loop was blocked"
    )
