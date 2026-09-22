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

---

**Reshaped at T47 (2026-09-22), after discovering it could not fail.** Three things changed, and the
third is the one worth reading.

1. *The sample floor no longer races the batch.* It used to be a bare `len(latencies) >= 20` over
   however many samples four fetches happened to leave room for — a precondition coupled to machine
   speed in the **wrong** direction, because a *fast* box finishes the batch before the sampler has
   its floor and fails a test whose property is holding comfortably. CI hit exactly that in slice 1.5
   (`test_export_inline.py`, 16 samples against a floor of 20, sampled latencies of 0.5 ms); this
   file carried the identical latent shape and had simply never been unlucky — probably because
   network I/O plus `trafilatura` is slow enough. It is now `loop_liveness.hammer_health_live_until_floor`'s own
   invariant: the sampler does not return short, whatever the batch does.
2. *A sample is the loop's **turnaround**, not the request's duration.* Timing
   `client.get("/health/live")` alone measures the one window in which the loop is by construction
   not blocked — an in-process ASGI request to a no-I/O route never reaches a real suspension point,
   so the loop cannot switch to a blocking worker in the middle of one. Lesson two in
   `loop_liveness.py` has the full account and the mutation that proved it.
3. *The four fetches now loop until the sampler has its floor, and the floor is 200.* Both changes
   are forced by the same measurement problem: **blocking suppresses sampling**, so a blocked loop
   under-represents itself in the sample set, and a p50 taken over a short window dominated by the
   fetches' I/O phase never sees the extraction phase at all.

**Mutation-verified, and the margin is stated rather than implied.** Replacing the
`asyncio.wait_for(asyncio.to_thread(self._extract_sync, html), ...)` in `fetching.py` step 12 with a
bare `self._extract_sync(html)` — the whole of what AC-10 guards — and changing nothing else:

| shape | healthy p50 | mutated p50 |
|---|---|---|
| as it stood before T47 (request-timed, one round, floor 20) | ~0.1 ms | **passed** — never red |
| turnaround-timed, one round, floor 20 | 0.59 ms | **passed** (6.54 ms once, then under budget) |
| turnaround-timed, sampler-driven, floor 200 (this file) | 0.46 / 0.50 / 0.54 ms | 6.63 / 16.03 / 18.68 / 273.74 ms |

So this assertion now fails against its own regression in 4 runs out of 4, with the healthy side
stable at roughly a tenth of the 5 ms budget. **The mutated side is not stable**: its lowest observed
figure, 6.63 ms, clears the budget by only 1.3x, so a fast enough box could still let this regression
through. That is a missed regression rather than a flaky red — the healthy side has 10x of headroom —
but it is a real limit and it is not the assertion's fault: p50 is the wrong statistic for a workload
whose blocked fraction is well under half. The statistic that would separate these cleanly is the
loop's *unavailable fraction* over the window (`sum(turnarounds) / wall-clock`), which is a change to
what AC-10 asserts and therefore a decision for AC-10's owner rather than for retention's T47.
**Owner: `qa`; trigger: the next slice that touches the fetcher, or the first time this test lets a
regression through.**

`tests/api/test_export_inline.py`'s AC-11 copy is **not** reshaped and **not** proven blind — its own
banner records that removing `asyncio.to_thread` from the renderer hangs the process outright, which
is a louder mutation than the one that fooled this file. Somebody should check it with a mutation
that only *partially* blocks.
"""

from __future__ import annotations

import asyncio
import http.server
import statistics
import threading
from collections.abc import Iterator

import pytest
from httpx import AsyncClient

from tailorcraft.domain.posting.value_objects import SourceUrl
from tailorcraft.infrastructure.posting.address_policy import TargetAddressPolicy
from tailorcraft.infrastructure.posting.fetching import HttpxTrafilaturaFetcher

from .loop_liveness import hammer_health_live_until_floor

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


# The sample floor this file needs, and it is ten times the shared default for a stated reason:
# **blocking suppresses sampling**, so a blocked loop under-represents itself, and a fetch is I/O
# *then* CPU — the extraction `asyncio.to_thread` protects the loop from does not begin until the
# download is done. Twenty samples are collected inside the first download phase, before the thing
# under test has started, and a p50 over them is a p50 over a warm-up. Two hundred spans several
# full fetch cycles per worker, which is what puts the median inside the extraction phase. Measured,
# not guessed: at floor 20 the mutation below passes; at floor 200 it fails 4 runs out of 4.
_SAMPLE_FLOOR = 200


async def _fetch_until_enough_samples(
    fetcher: HttpxTrafilaturaFetcher, url: str, *, enough: asyncio.Event
) -> None:
    """Keep fetching until the sampler has its floor, checked before every fetch rather than after —
    an in-flight fetch always finishes, so `enough` being set mid-fetch costs at most one extra
    fetch per worker rather than a torn result. The page is served by a local stub, so looping costs
    nothing but CPU and touches no network."""
    while not enough.is_set():
        await fetcher.fetch(SourceUrl(url))


async def test_loop_turnaround_p50_stays_under_5ms_during_four_concurrent_fetches(
    client: AsyncClient, large_page_server: _LargePageServer
) -> None:
    """AC-10: sample the loop's turnaround while four concurrent fetches of a large page loop, and
    assert its p50 stays under 5 ms — the same budget ADR-0009's addendum used, so the numbers are
    comparable in units. See the module docstring for what a sample is, what this file's reshaping
    fixed, and exactly how far this assertion's mutated margin extends."""
    fetchers = [_fetcher() for _ in range(4)]
    enough = asyncio.Event()

    hammer_task = asyncio.ensure_future(
        hammer_health_live_until_floor(client, enough=enough, floor=_SAMPLE_FLOOR)
    )
    await asyncio.sleep(0)  # let the sampling task actually start before the fetches begin

    # `enough.set()` in `finally`, not after a bare `await gather(...)`: if any fetch raises, a bare
    # sequence would skip straight past it and leave the workers that are still running looping
    # forever on this session-scoped event loop — a real failure mode hit while writing this test (a
    # first draft of the fixture page made every fetch raise `SourceTextTooLong`, and the resulting
    # orphaned task looked exactly like a hang).
    try:
        # A generous but finite ceiling, not a tuned number: `enough` is set by the sampler the
        # instant it has its floor. It is deliberately wide enough to let the *mutated* fetcher
        # finish and fail on the assertion below — a timeout here would report "a fetch took too
        # long", which is a throughput claim this test does not make.
        await asyncio.wait_for(
            asyncio.gather(
                *(
                    _fetch_until_enough_samples(fetcher, large_page_server.url, enough=enough)
                    for fetcher in fetchers
                )
            ),
            timeout=120,
        )
    finally:
        enough.set()
    turnarounds = await asyncio.wait_for(hammer_task, timeout=15)

    # A self-check on `hammer_health_live_until_floor`'s own invariant, not a precondition that can lose a race —
    # this is the 1.5 flake, fixed (T47); see the module docstring's item 1.
    assert len(turnarounds) >= _SAMPLE_FLOOR, (
        f"only {len(turnarounds)} /health/live samples were taken during the four concurrent "
        "fetches — the sampler returned before reaching its own floor, which should be impossible; "
        "see loop_liveness.hammer_health_live_until_floor"
    )

    p50 = statistics.median(turnarounds)
    # p50 only, no `max(...)` assertion: four concurrent CPU-bound Python threads contending for the
    # GIL produce real spikes no implementation under this port can prevent — the healthy runs above
    # recorded maxima of 29 ms and 368 ms while their p50s sat at half a millisecond. `max` is
    # reported as diagnostic context, exactly as its two sibling measurements do.
    assert p50 < 0.005, (
        f"/health/live turnaround p50 was {p50 * 1000:.2f} ms during four concurrent fetches "
        f"(n={len(turnarounds)}, max={max(turnarounds) * 1000:.2f} ms) — the event loop was blocked"
    )
