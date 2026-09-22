"""AC-42 (T47) — the event loop is not blocked while `LocalOrphanFileScanner` walks a large volume.

**What this measures: event-loop LIVENESS, not scan throughput.** `LocalOrphanFileScanner`'s walk is
a synchronous generator resumed one chunk at a time inside `asyncio.to_thread`
(`infrastructure/files/orphan_scanner.py`), so the `os.scandir` and `DirEntry.stat` calls happen on a
worker thread and the awaiting coroutine gets a suspension point roughly every `_DEFAULT_CHUNK_SIZE`
entries. That buys the loop the ability to keep answering *other* requests while a sweep runs; it
does not buy the sweep itself any speed. This test asserts the loop stayed responsive and asserts
nothing about how fast the scans finished.

**Why this adapter earns its own liveness test rather than inheriting the sibling ones.** The two
established measurements in this codebase cover CPU-bound work in a thread — `trafilatura`'s parse
(AC-10, `test_posting_fetcher_event_loop.py`) and markdown-it's (AC-11,
`tests/api/test_export_inline.py`). This one covers **syscall**-bound work, and it is the first
caller in the codebase whose walk spans a whole volume rather than one file: a sweep of the dev
uploads volume's 827 files is small, the production shape is unbounded, and on the loop it would
stall every concurrent user for the length of the walk with no error and nothing logged. That is the
failure this codebase treats as CRITICAL precisely because it presents as "the app is slow" rather
than as anything a health check or a log line would show.

It is also the failure mode `test_orphan_scanner.py` says it does not cover — that file is about the
scanner's *answer* (does a planted symlink come back `ref=None`), this one is about the *thread*.

**Sizing is a guarantee, not a guess**, and the reasoning lives once in `loop_liveness.py`: the
sampler sets `enough` the instant it has its floor, and every scanning worker checks that event
before starting its next scan, so the batch's duration is an output of the measurement rather than an
iteration count that can drift out of date as the machine gets faster. The 1.5 CI flake this
criterion warns about — a fast runner finishing a fixed batch before the sampler had its 20 samples —
is unrepresentable in this shape.

**What the samples are, and why the first version of this file was worthless** (2026-09-22). A
sample is the loop's *turnaround* — a paced sleep plus a `GET /health/live`, minus the sleep — not
the request's own duration. The distinction is the whole test. Written the obvious way, timing only
the request, this file **passed against `asyncio.to_thread` removed from the scanner's walk**: p50
under a millisecond, against the exact defect AC-42 exists to catch. An in-process ASGI request to a
route that does no I/O never reaches a real suspension point, so the loop cannot switch to a blocking
worker in the middle of one — a request that starts while the loop is free finishes while the loop is
free, at full speed, no matter how long the loop was blocked on either side of it. Lesson two in
`loop_liveness.py` has the full account; it is a defect in the *measurement*, and it is available to
every test in this codebase that samples an in-process route.

**Mutation-verified 2026-09-22, and the numbers are the point.** With the walk in a thread: p50
**0.62 ms**, max 3.78 ms. With `await asyncio.to_thread(_next_chunk, walker)` in `scan_older_than`
replaced by a bare `_next_chunk(walker)` and nothing else changed: p50 **1263.71 ms**, max 1883 ms —
red, by a factor of 250 against the 5 ms budget and 2,000 against the healthy figure. Source restored
byte-exact afterwards (`diff` clean). Note the mutated run also takes 28 s instead of 3 s; wall-clock
was visible all along, which is exactly why the p50 passing was worth catching.
"""

from __future__ import annotations

import asyncio
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest
from httpx import AsyncClient

from tailorcraft.infrastructure.files.orphan_scanner import LocalOrphanFileScanner

from .loop_liveness import SAMPLE_FLOOR, hammer_health_live_until_floor

pytestmark = pytest.mark.slow

# A cutoff far enough in the future that every entry on the tree clears it. Deliberate: the heaviest
# and most honest shape for this measurement is the one a real sweep of an old volume produces, where
# `_describe` builds a `ScannedFile` — and runs `FileRef`'s grammar `fullmatch` — for every entry it
# finds, rather than discarding most of them on the age check before any of that work happens. A
# constant rather than `datetime.now()`-plus-a-margin: nothing here needs a clock (`Clock` exists for
# the code that does), and a fixed value cannot make this test's workload depend on the calendar.
_FAR_FUTURE_CUTOFF: Final = datetime(2100, 1, 1, tzinfo=UTC)

# Large enough to span many chunk boundaries (`_DEFAULT_CHUNK_SIZE` is 512, so this is 16 thread
# hops per scan) and to be a "large scan" against the only real number available — the dev uploads
# volume currently holds 827 files. Measured in this container: the tree costs ~0.9 s to build and
# ~0.3 s to walk, which is what keeps a `slow` test at a few seconds rather than a minute.
_FILE_COUNT: Final = 8192

# Concurrent sweeps. More than one because the claim is about *contention* — a single scan that
# yields politely between chunks proves less than several doing it at once — and four rather than
# twenty because these threads are syscall-bound and release the GIL, so four is already enough to
# keep the default executor busy without the measurement turning into one about thread-pool sizing.
_CONCURRENT_SCANS: Final = 4

# The same budget AC-10 and AC-11 assert, so the numbers are comparable — with the caveat that
# AC-11's copy still times the request alone (see `loop_liveness.py`, lesson two), so its number is
# comparable in units and not yet in meaning.
_P50_BUDGET_SECONDS: Final = 0.005


@pytest.fixture(scope="module")
def large_volume(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A file store laid out exactly as `LocalFileStore` writes one: `<2 hex>/<2 hex>/<uuid>.pdf`,
    with the shard names taken from the file's own id.

    Built faithfully rather than conveniently. A synthetic layout — a handful of fat directories, say
    — would be cheaper to create and would measure a tree this store never writes: real keys are
    UUIDs scattered over 65,536 possible shards, so a sparse volume is mostly *directories*, and
    `os.scandir` per directory is the syscall this walk actually spends its time in.

    Module-scoped because it is read-only and costs ~0.9 s; `tmp_path_factory` rather than `tmp_path`
    is what that scope requires.
    """
    root = tmp_path_factory.mktemp("uploads")
    for _ in range(_FILE_COUNT):
        generated = uuid4()
        hex_digits = generated.hex
        shard = root / hex_digits[0:2] / hex_digits[2:4]
        shard.mkdir(parents=True, exist_ok=True)
        (shard / f"{generated}.pdf").write_bytes(b"not read by the scanner, which only stats")
    return root


async def _scan_until_enough_samples(
    scanner: LocalOrphanFileScanner, *, enough: asyncio.Event
) -> None:
    """Keep scanning until the sampler has its floor, checked before every scan rather than after —
    an in-flight scan always finishes, so `enough` being set mid-walk costs at most one extra scan
    per worker rather than a torn result.

    The count assertion is not incidental. Without it this test would pass just as happily against an
    empty directory, which is the shape a liveness measurement fails vacuously in: a walk that finds
    nothing cannot block a loop, and the p50 below would be measuring an idle process.
    """
    while not enough.is_set():
        scanned = await scanner.scan_older_than(_FAR_FUTURE_CUTOFF)
        assert len(scanned) == _FILE_COUNT, (
            f"a scan found {len(scanned)} entries on a volume of {_FILE_COUNT} — the measurement "
            "below would be about an idle process, not a large sweep"
        )


async def test_loop_turnaround_p50_stays_under_5ms_during_four_concurrent_volume_scans(
    client: AsyncClient, large_volume: Path
) -> None:
    """AC-42: sample the loop's turnaround on `/health/live` while four concurrent orphan scans of an
    8,192-file volume run, and assert its p50 stays under 5 ms. See the module docstring for what a
    sample is and for the mutation that proves this assertion can fail."""
    scanners = [LocalOrphanFileScanner(large_volume) for _ in range(_CONCURRENT_SCANS)]
    enough = asyncio.Event()

    hammer_task = asyncio.ensure_future(hammer_health_live_until_floor(client, enough=enough))
    await asyncio.sleep(0)  # let the sampling task actually start before the scans begin

    # `enough.set()` in `finally`, not after a bare `await gather(...)`: if a scan raises, a bare
    # sequence would skip past it and leave the workers that are still running looping forever on
    # this session-scoped event loop — an orphaned task that looks exactly like a hang. The sampler
    # stops itself, but the workers only stop on this event.
    try:
        # A generous but finite ceiling, not a tuned number: `enough` is set by the sampler the
        # instant it has its floor, so this bounds only the pathological case where that never
        # happens rather than deciding how long a healthy run takes. It is deliberately wide enough
        # to let the *mutated* scanner finish and fail on the assertion below — a timeout here would
        # report "a scan took too long", which is a throughput claim this test does not make.
        await asyncio.wait_for(
            asyncio.gather(
                *(_scan_until_enough_samples(scanner, enough=enough) for scanner in scanners)
            ),
            timeout=120,
        )
    finally:
        enough.set()
    turnarounds = await asyncio.wait_for(hammer_task, timeout=15)

    # A self-check on `hammer_health_live_until_floor`'s own invariant, not a precondition that can lose a
    # race: it does not return until it has collected `SAMPLE_FLOOR` samples.
    assert len(turnarounds) >= SAMPLE_FLOOR, (
        f"only {len(turnarounds)} /health/live samples were taken during the "
        f"{_CONCURRENT_SCANS} concurrent scans — the sampler returned before reaching its own "
        "floor, which should be impossible; see loop_liveness.hammer_health_live_until_floor"
    )
    p50 = statistics.median(turnarounds)
    # p50 only, no `max(...)` assertion — the same considered choice both sibling measurements make,
    # for the same reason: a max-latency bound would be an assertion about CPython's scheduler under
    # thread contention rather than about this adapter. `max` is reported as diagnostic context.
    assert p50 < _P50_BUDGET_SECONDS, (
        f"/health/live turnaround p50 was {p50 * 1000:.2f} ms during {_CONCURRENT_SCANS} concurrent "
        f"scans of {_FILE_COUNT} files (n={len(turnarounds)}, max={max(turnarounds) * 1000:.2f} ms) "
        "— the event loop was blocked"
    )
