"""The sampler-driven `/health/live` probe shared by this package's two loop-liveness measurements.

**Why this is a module and not a third copy.** Two files here measure the same property against two
different adapters — `test_posting_fetcher_event_loop.py` (AC-10, the fetcher's extraction step) and
`test_orphan_scanner_event_loop.py` (AC-42, the orphan scanner's directory walk) — and a third
in-place copy lives in `tests/api/test_export_inline.py` (AC-11, the inline renderer). What is shared
here is the *sampler*, which is the part both lessons below are about: it must be identical in every
copy or the lesson is only half-learned.

---

**Lesson one: the sampler drives the batch, it does not race it (the 1.5 CI flake).**

A liveness measurement needs a minimum number of samples before a p50 means anything, and the obvious
way to get them — run a fixed batch of work and hope the sampler keeps up — couples a test
*precondition* to how fast the machine happens to be. That flake is bidirectional: a slow box blows
the latency budget (the property under test), and a **fast** box finishes its fixed batch before the
sampler has its floor and trips the precondition instead. CI hit the second case in slice 1.5 — 16
samples against a floor of 20, with sampled latencies of 0.5 ms, i.e. the property under test holding
comfortably while its scaffolding failed.

So `hammer_health_live_until_floor` sets the shared `enough` event the instant it has `floor`
samples, every worker checks that event before starting its next unit of work, and the batch's
duration becomes an **output** of the measurement instead of an assumption baked into an iteration
count. The floor assertion in each caller is then a self-check on an invariant this function
guarantees, not a race it might lose.

---

**Lesson two: timing the request alone cannot see a blocked loop, so this times the turnaround**
(found 2026-09-22, by mutation-testing AC-42's new test against the regression it claimed to guard).

The first version of this sampler timed `client.get("/health/live")` and nothing else. It passed
against a `LocalOrphanFileScanner` with `asyncio.to_thread` removed from its walk — i.e. against the
exact defect AC-42 exists to catch — with a p50 well under a millisecond. The test took 27 s instead
of 3 s, so the blocking was real and enormous; the measurement simply could not see it.

The reason is the same fact that makes the pacing sleep load-bearing (below): `client` is an
in-process `httpx.ASGITransport` client and `/health/live` does no I/O of any kind, so a request
against it resolves through nested `await`s that **never hit a real suspension point**. The loop
therefore cannot switch to a blocking worker in the middle of one. A request that begins while the
loop is free completes while the loop is free, at full speed, however long the loop was blocked
before it started and however long it will be blocked after. Timing only the request measures the one
window in which the loop is by construction not blocked.

What a blocked loop actually delays is *getting a turn at all* — so a sample spans the turn: the
pacing sleep **plus** the request, minus the sleep's nominal duration. What is left is the loop's own
turnaround, how late the timer fired plus how long the request then took. A healthy loop answers in a
fraction of a millisecond; a loop inside a 300 ms synchronous directory walk cannot answer until it
is finished, and the number says so (0.62 ms healthy against 1263 ms mutated, for AC-42).

**A corollary neither caller may forget: blocking suppresses sampling.** A blocked loop takes no
samples while it is blocked, so it under-represents itself in the very set being summarised, and a
p50 is therefore only sensitive when the blocked fraction is comfortably over half. AC-42's workload
blocks from the first instant and satisfies that; AC-10's is I/O *then* CPU and does not, which is
why that file raises its floor to 200 and still records a margin rather than claiming one. A
statistic with no such weakness — the loop's unavailable *fraction*, `sum(turnarounds)` over the
window's wall-clock — exists and is the right eventual answer; adopting it changes what those
criteria assert, so it was recorded in `test_posting_fetcher_event_loop.py` with an owner rather than
done there.

**AC-20 (`tests/integration/adapters/test_login_event_loop.py`, T34) hit exactly this weakness and
adopted the fix, on its owner's decision.** Eight concurrent logins against a real, production-cost
argon2 verify are *mostly* async I/O (two Postgres round trips and a Redis check) with *one*
CPU-bound step, so the blocked fraction under that concurrency stayed under half and p50 never went
red against the executor-hop mutation across five separate runs, even though `max` and the batch's
own wall-clock both showed the regression plainly. `unavailable_fraction` below is what that file
gates on now; p50 is still computed and reported alongside it, never as the assertion.

**AC-11's copy in `tests/api/test_export_inline.py` is not fixed and not proven blind.** Its own
banner records that removing `asyncio.to_thread` from the renderer hangs the process outright, which
is a louder mutation than the one that fooled this sampler. Somebody should check it with a mutation
that only *partially* blocks. Owner: `qa`; trigger: the next slice that touches inline rendering.
"""

from __future__ import annotations

import asyncio
import time

from httpx import AsyncClient

# The default sample-size floor a p50 needs before it means anything. A caller whose workload has a
# warm-up phase the samples must get past raises it — and says why, with the measurement that made
# the old number wrong (see `test_posting_fetcher_event_loop.py`).
SAMPLE_FLOOR = 20

# The pacing delay, and it is load-bearing in two separate ways — see the docstring below for the
# starvation it prevents, and lesson two above for why the sample is measured across it.
PACE_SECONDS = 0.001


async def hammer_health_live_until_floor(
    client: AsyncClient, *, enough: asyncio.Event, floor: int = SAMPLE_FLOOR
) -> list[float]:
    """Sample the event loop's turnaround until `floor` samples are collected, then set `enough`.

    One sample is `sleep(PACE) + GET /health/live`, minus `PACE` — the loop's own latency in
    answering a request that costs it nothing, including how long it took to get around to it. See
    lesson two above for why the request cannot be timed on its own, and why a number that looks
    like "request latency" would be a number that cannot fail.

    `enough` is the single stop condition for this loop *and* for every worker in the batch being
    measured, which is lesson one: the sampler ends the batch, so the batch can never end the
    sampler short.

    **The pacing sleep is load-bearing for a second, independent reason.** Without an explicit yield,
    this loop's own `Task` starves every other task on the loop **forever**, including the workers it
    is meant to be measuring — found the hard way in slice 1.2, where a bare `await asyncio.sleep(0)`
    in this position hung indefinitely, because `sleep(0)` only reschedules onto the *current*
    ready-queue pass and this loop's next iteration was consistently ready before the sibling task's
    continuation. A small positive delay is what actually lets the workers get scheduled turns, and
    it caps the hammering rate to something sane instead of an unbounded tight loop.
    """
    turnarounds: list[float] = []
    while not enough.is_set():
        started = time.perf_counter()
        await asyncio.sleep(PACE_SECONDS)
        response = await client.get("/health/live")
        turnarounds.append(time.perf_counter() - started - PACE_SECONDS)
        assert response.status_code == 200
        if len(turnarounds) >= floor:
            enough.set()
    return turnarounds


def unavailable_fraction(turnarounds: list[float], *, baseline_seconds: float = 0.0) -> float:
    """The share of the sampler's own answered time that was **excess beyond `baseline_seconds`**:
    `sum(max(0, t - baseline)) / sum(turnarounds)` (module docstring, lesson two's corollary, twice
    amended below — read both amendments before using this on a new workload).

    Pure and synchronous — it does not sample anything itself. Unlike p50, this statistic is not
    blind to a blocked loop that answers *most* samples quickly and a few very slowly: a blocked
    sample still contributes its full delay to the sum, so a workload whose blocked fraction is well
    under half — the exact shape that defeated p50 for AC-20 — still moves this number in proportion
    to how much of the sampled time was actually lost. Bounded to `[0, 1]` by construction (`excess
    <= t` for every `t >= 0`, so the sum in the numerator can never exceed the sum in the
    denominator), which the first version below was not.

    **Amendment one — `baseline_seconds` (lesson three, found hours after the first version
    shipped).** A caller that hands raw `turnarounds` with no baseline measures *latency*, not
    *blocking*, because a healthy loop's turnaround is not zero — it is the ambient per-request cost
    of answering `/health/live` at all, call it `t̄`. Subtracting `baseline_seconds` (the median
    turnaround measured with **nothing else in flight**, on the same box, right before the workload
    under test) removes that ambient cost before summing, so a uniformly slower box — higher baseline
    *and* higher loaded turnarounds, moving together — contributes close to zero excess, while
    genuine blocking, which grows turnarounds *relative to that same box's own baseline*, still shows
    up in full. The default `0.0` is the original, baseline-free statistic (harmless for a caller
    with no baseline available, since every turnaround is already non-negative).

    **Amendment two — normalised by `sum(turnarounds)`, never by wall-clock (found the same day,
    measuring amendment one against a changed `PACE_SECONDS`).** An earlier version of this function
    divided by the caller's own measured wall-clock span instead. For a single-coroutine, paced
    sampler, `wall_clock ≈ N x (PACE_SECONDS + t̄)` for `N` samples — so a larger `PACE_SECONDS`
    inflates the denominator with sampler-induced *idle* time that has nothing to do with blocking,
    and the healthy value measurably shrinks purely because the sampler was told to sleep longer
    between requests (measured: raising `PACE_SECONDS` five-fold roughly halved the healthy value
    under wall-clock normalisation, on a workload whose genuine blocked share had not changed at
    all — the very "precondition scales with an unrelated knob" shape this whole file exists to
    avoid). `sum(turnarounds)` excludes `PACE_SECONDS` by construction (each turnaround already has
    it subtracted, per sample, in `hammer_health_live_until_floor`), so this ratio is answered-time
    against answered-time and the pace no longer enters it *arithmetically*. It is not
    invariant to the pace in practice, though: re-running the same healthy workload at five times
    the pace moved the healthy value from the 0.775-0.799 cluster to **0.564** (a sparser poll samples
    the same contention with a different density), which is why `PACE_SECONDS` is a module
    constant and the bound was measured at it — see `test_login_event_loop.py`'s own record.

    A caller still measures its own bound empirically for its own workload — this ratio removes ambient
    latency and takes sampler pacing out of the arithmetic (not out of the measurement, above) — it
    does not remove the need for an empirical bound.
    """
    total = sum(turnarounds)
    if total <= 0:
        raise ValueError(f"sum(turnarounds) must be positive, got {total!r}")
    excess = sum(max(0.0, t - baseline_seconds) for t in turnarounds)
    return excess / total
