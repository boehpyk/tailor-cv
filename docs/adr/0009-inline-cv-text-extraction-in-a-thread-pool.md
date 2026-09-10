# ADR-0009: CV text extraction runs inline in a thread pool, with a named latency exception

- **Status:** Accepted
- **Date:** 2026-09-07

## Context

Slice 1.1 (`intake-base-cv-upload`) accepts a PDF, DOCX or TXT file and turns it into plain text so
later slices have something to tailor. Extraction uses `pypdf` and `python-docx`, both of which are
**synchronous and CPU-bound**. That collides with two rules already on the books.

**ADR-0005** sends PDF and DOCX *rendering* to a Celery worker, and states the rule as **the cost of
the work, not the tidiness of treating similar things alike**. The tempting move is to read "PDF work
goes to Celery" off that ADR and stop thinking. But the two jobs are not the same size: WeasyPrint
composes a document, resolves fonts and rasterises — seconds. `pypdf` walks a content stream and
concatenates text — a 10 MB text-layer CV lands around 200–800 ms.

**Constitution §1** says a synchronous CPU-bound call inside an async route blocks the event loop for
every concurrent user, and that the failure is silent: fine with one user, collapsing with five,
presenting as "the app is slow" rather than as an error. So "just call `pypdf` in the route" is not
on the table either.

**Constitution §7** budgets non-LLM, non-export API responses at **< 300 ms p95**. Inline extraction
does not fit inside that number, and this is the first slice where a stated budget and a stated
design pull in opposite directions.

There is also a product force. This is the **first product slice**. Queueing extraction would mean a
job row, a status endpoint, a client poll loop and a staged-progress UI — in the slice whose actual
job is to establish the domain model. The staged progress UI is slice 1.4's design work, and doing it
badly in 1.1 means doing it twice.

## Decision

**1. Extraction runs inline, in a worker thread.** The `CvTextExtractorPort` adapter wraps the
synchronous library call in `asyncio.to_thread(...)` inside `asyncio.wait_for(..., timeout=10)`. The
event loop is never blocked; the request simply awaits. No Celery task, no queue, no job row, no
polling in the browser. `POST /api/base-cvs` answers `201` with the outcome already decided.

**2. The work is bounded before it starts, not discovered by the timeout.** The 10 MB upload cap is
enforced before the body is read (see the addendum — the original wording here said "while streaming",
which turned out not to be achievable as stated), and a PDF with more than 50 pages is refused
*before* parsing. The 10 s
timeout is the backstop for a pathological file, not the mechanism. A timeout that fires routinely is
a capacity plan, not a safety net.

**3. A named exception to Constitution §7 is recorded, not quietly missed:**

> **intake upload + extraction: p95 < 3 s server-side, for files ≤ 10 MB.**

The 300 ms budget continues to bind every other non-LLM, non-export endpoint, including the two reads
this slice adds. §7's table gains a row pointing here. A budget openly revised is a budget; a budget
quietly missed is a lie that gets discovered during an incident.

**4. This decision is revisited when the corpus says so, not when it feels slow.** If p95 measured
against the 20-layout corpus (PRD §10, validation 1) exceeds 3 s, or if extraction failures exceed
10% of layouts, that is the trigger for a follow-up ADR proposing the queue — or a different library.
Written down now so the revisit is a criterion rather than a mood.

## Alternatives

- **Queue extraction to Celery, return `202`, poll for the result.** The serious alternative, and
  rejected on cost rather than on principle. It honours §7 for free, makes the `/health/ready` Celery
  probe load-bearing from day one, and establishes the polling pattern slice 1.5 needs anyway. It also
  adds a job-state row, a status endpoint, three failure-contract rows (worker down, task retried, job
  stuck in `queued`) and a polling React surface to the first slice in the codebase. **If extraction
  ever grows expensive — OCR is the obvious way that happens — this is the alternative to revisit, and
  the `CvTextExtractorPort` boundary is exactly what makes it a later adapter swap rather than a
  rewrite.**
- **Call `pypdf` directly in the async route.** Rejected outright. It is the silent event-loop block
  Constitution §1 names as CRITICAL, and it would look perfectly fine in every test we write.
- **Extract lazily, on first use by the tailoring slice.** Rejected: it defers the user's bad news.
  A scanned CV with no text layer is a problem the user can fix in ten seconds at upload time, and
  cannot fix at all after waiting fifteen seconds for a tailoring run that had nothing to work with.
- **Amend Constitution §7's global budget to 3 s.** Rejected. The 300 ms number is right for the other
  endpoints and relaxing it globally to accommodate one route would remove the pressure that keeps
  them fast.

## Consequences

- **The thread pool is now a shared, finite resource.** `asyncio.to_thread` uses the default executor;
  concurrent uploads consume its threads. Under the rate limits chosen for this slice (10/h/session,
  30/h/IP) the ceiling is not close, but this is the number to look at first if uploads ever queue
  behind each other. Worth a metric before it is worth a fix.
- **An abandoned extraction thread outlives its request.** `wait_for` cancels the *await*, not the
  thread — a pathological file can keep a worker thread busy after the client has its `201`. Bounded
  by the page and size caps, and noted here so nobody is surprised by it in a profile.
- The `/health/ready` Celery probe stays honest but **is not exercised by any user-facing path until
  slice 1.5**. A stopped worker still shows red; nothing in Phase 1.1 breaks when it does.
- Adding OCR later is a genuine re-open of this ADR, not an enhancement to the adapter. OCR is seconds
  to tens of seconds and belongs on the queue. Say so at the time.
- The `p95 < 3 s` figure needs a measurement to be real. It is an acceptance criterion (AC-6) of slice
  1.1, checked against the fixture corpus — not an aspiration in a document.

---

## Addendum: the measurement (2026-09-08, at slice 1.1's `/verify`)

This ADR closed by saying the `p95 < 3 s` figure "needs a measurement to be real". It now has one.
The numbers are recorded here rather than in a commit message because the *next* person to weigh
inline-versus-queued needs the baseline, and a number nobody can find is a number nobody trusts.

**Method.** A 2 MB, 40-page PDF with a genuine text layer (80,080 extractable characters), built for
the purpose and deliberately just under the 50-page cap so the measurement exercises the real parsing
path rather than the refusal path. Twenty uploads through the full ASGI stack — middleware, cookie
minting, sniffing, file store, extraction, commit — after three warm-up requests, because a cold
first call is not what p95 means. Each upload got a fresh guest session; the first attempt ran into
the five-CV-per-session cap, which is the cap working correctly and a benchmark measuring the wrong
thing.

| | |
|---|---|
| n | 20 |
| min | 0.277 s |
| p50 | 0.292 s |
| **p95** | **0.302 s** |
| max | 0.323 s |
| extraction alone (`duration_ms`) | ~260 ms |

**The budget holds with an order of magnitude to spare** — 0.302 s against 3 s. Worth noticing: it
also lands within a rounding error of Constitution §7's ordinary 300 ms budget, which means the named
exception this ADR bought has, so far, gone almost unspent. Do not read that as "the exception was
unnecessary". It was bought for the pathological end of the range — a 10 MB scan-heavy file — and the
corpus does not yet contain one. It is insurance whose premium came due once and was cheap.

### The finding worth keeping: threads are not parallelism

The second half of §1's rule is that extraction must not block the event loop, and that was measured
separately by hammering `/health/live` while uploads ran.

| concurrent uploads | `/health/live` p50 | max |
|---|---|---|
| 1 | 0.4 ms | 15.7 ms |
| 2 | 0.6 ms | 279.9 ms |
| 4 | 0.6 ms | 322.6 ms |

**The loop is never blocked for the duration of an extraction** — that is the property `asyncio.to_thread`
buys, and it holds. A synchronous call in the route would have produced stalls the full ~260 ms ×
concurrency, not a p50 under a millisecond.

But the tail tells the more useful story. At four concurrent uploads the health check occasionally
waits a third of a second, and four concurrent extractions took **1.343 s wall** where four serial
ones would take ~1.16 s — *slower than serial, not faster*. `pypdf` is pure Python, so the GIL
serialises the work no matter how many threads it is spread across; the threads buy event-loop
liveness, not throughput, and they charge a little interpreter contention for it.

This is not a defect and nothing here needs fixing. It is the shape of the trade this ADR made, now
with numbers on it, and it sharpens the revisit trigger above: **the thing that will force the queue
is concurrency, not file size.** A single big file is comfortable. Ten simultaneous uploads on one
box will not be, and the symptom will be tail latency on unrelated endpoints — which is exactly the
symptom nobody attributes to the upload endpoint. If a metric is ever added here, make it the p99 of
something *other* than the upload route.

---

## Addendum, 2026-09-09 — the failure translation is a floor, not a list (from `/verify` on slice 1.1)

This ADR put extraction behind `CvTextExtractorPort`, whose contract is that **every** failure
arrives as a `CvExtractionFailed` subclass — that is what lets `UploadBaseCv` record a failed
extraction as a state instead of leaking a 500 (ADR-0004). The first implementation honoured that
contract with an allow-list: `PdfReadError`, `FileNotDecryptedError`, `BadZipFile`,
`PackageNotFoundError`.

An allow-list makes the contract a **bet** that we enumerated every way `pypdf` and `python-docx` can
fail on a file chosen by a stranger, and review showed the bet already losing. A sweep of 300
randomly byte-corrupted PDFs produced 21 escapes — `KeyError`, `AttributeError`, `ValueError`,
`pypdf.errors.LimitReachedError` — each becoming a 500 with the upload already on disk and no row.
The same shape existed in `sniffing._is_docx`, whose docstring promised "never raises" while catching
only `BadZipFile`; a malformed zip central directory raises `struct.error`, `NotImplementedError`,
`EOFError` or `OverflowError` depending on which field the corruption lands in, turning a file that
merely *isn't a DOCX* (415) into a 500.

**Decision.** The specific translations stay and still run first, because they carry a reason the user
can act on. Beneath them sits an `except Exception` floor mapping to `EXTRACTOR_ERROR`, at
`extract()` level rather than per-format so it also covers `_decode_txt` and the character counting.
`Exception`, not `BaseException`: `asyncio.CancelledError` must still cancel rather than be recorded
as a failed extraction, and a test now pins that.

**Two privacy rules attach to that floor**, both Constitution §8:

1. **Only the exception's fully-qualified type is logged**, on a separate `cv_extraction.unexpected_error`
   event that leaves `cv_extraction.finished`'s AC-12 field set untouched. Never `str(exc)`, never
   `exc_info` — `pypdf` quotes raw document bytes in several of its own messages.
2. **`raise ... from None`.** The frame holds `data: bytes` and `raw_text: str`. `sentry_sdk` defaults
   `include_local_variables=True` — a setting `send_default_pii=False` does not touch — so a chained
   exception would carry the CV itself into a report. Verified against the installed SDK:
   `walk_exception_chain` branches on `__suppress_context__` and stops when `__cause__` is `None`.

**One narrow mapping worth recording, because it will look wrong later.**
`pypdf.errors.DependencyError` maps to `EncryptedCvFile`, not the floor. `PdfReader.__init__`
auto-attempts an empty-password decrypt whenever `is_encrypted` is true — *before* the adapter's own
`is_encrypted` branch runs — and with the optional `cryptography` package deliberately absent, every
AES-encrypted PDF raises `DependencyError` from the constructor. Such a file genuinely is
password-protected, so `encrypted` ("remove the password") is the actionable answer where the floor
would say "try again", which cannot work. **This is correct only because the `try` calls nothing but
`extract_text()`** — `pypdf` also raises `DependencyError` for JBIG2 *image* decoding. The OCR
re-open named above would make that path reachable and start telling owners of unencrypted scans to
remove a password they never set. Narrow the clause then.

**Consequence for the revisit trigger.** Nothing here changes the queue-vs-inline decision. It does
change what "extraction failed" means operationally: `extractor_error` is now reached by two causes —
the 10 s timeout and an unrecognised library failure — so a rise in that reason is no longer
automatically a latency signal. The `error_type` field on `cv_extraction.unexpected_error` is what
separates them.
