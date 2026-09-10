# ADR-0013: The job-posting intake surface — one endpoint, two sources, and no row for a failed fetch

- **Status:** Accepted
- **Date:** 2026-09-09
- **Relates to:** ADR-0004 (a failed run is a recorded state, never a 500 with nothing on disk),
  ADR-0007 (persistence conventions), ADR-0012 (the guarded egress this endpoint drives).
  Supersedes nothing. **Deliberately contradicts the shape slice 1.1 established** — which is most of
  why it exists.

## Context

Slice 1.2 accepts a job description two ways: the user pastes text, or the user gives a link we
fetch. Two questions had to be answered before any code was written, and they turn out to be one
question wearing two hats: **what is a `JobPosting`, and how does one come to exist?**

1. **One endpoint with a tagged-union body, or two endpoints?** (OQ-1)
2. **Does a fetch that fails leave a row behind?** (OQ-3)

The second question is the interesting one, because slice 1.1 answered the analogous question the
*other* way and did so on purpose. `UploadBaseCv` always creates a `BaseCv` row; a failed text
extraction becomes `status = extraction_failed` on that row, with a reason — the ADR-0004 shape,
executed early so that 1.3's `TailoringRun` has a precedent to copy.

A reader arriving here straight from 1.1 will expect the same shape, will not find it, and — this is
the actual risk — will "fix" it. Recording the reason in an ADR rather than in a spec that dies with
the slice is the only thing that outlives the ten minutes it would take to make that change.

## Decision

### 1. One endpoint, `POST /api/job-postings`, with a tagged union on `source`

```json
{"source": "pasted",  "text": "We are looking for a senior Python engineer…"}
{"source": "fetched", "url":  "https://jobs.example.com/postings/1234"}
```

Pydantic v2's `Field(discriminator="source")` on the wire; a `match` over two frozen command
dataclasses in the use case.

Both bodies produce **the same resource** through the same use case, under the same authorization
rule, the same per-session cap and the same domain event. Two endpoints would be two places for all
four to drift, and drift between two paths into one table is not a hypothetical — it is the ordinary
outcome of a year of small changes made to one of them.

The client's intent is "here is the job description". *How it arrived* is a property of the input,
not a different intention.

**The counter-argument is real and is recorded rather than dismissed.** Two paths would let an edge
proxy rate-limit the SSRF-bearing one by URL alone, without parsing a body, and would give each path
a narrow `responses=` map instead of one that must list every failure either arm can produce. If an
edge-level control on outbound fetching is ever needed, splitting the route is the change to make,
and this paragraph is the note saying it was foreseen.

### 2. A failed fetch creates nothing

A fetch that fails is an HTTP error with a stable `code` and FR-2's paste fallback. It is **not** a
`JobPosting` in a failed state. Four reasons, in decreasing order of force:

**a. There is no artifact.** This is the load-bearing one. A failed extraction still leaves a
`BaseCv` row because that row is *the receipt for bytes we are holding on a volume* — the file
exists, so something must own it, and the purge must be able to find it (ADR-0006 §2). A failed
fetch holds nothing at all: no bytes, no text, no file, nothing on any volume. A row would record an
*event*, and this codebase already has domain events for recording events.

**b. The invariant is what makes slice 1.3 simple.** "A `JobPosting` always has usable text" means
the tailoring use case never asks *is this posting's text actually there?* — the type answers it.
Admitting a textless `JobPosting` pushes a `None` check into every consumer, forever, and the check
is only ever false for rows nobody wants.

**c. ADR-0004's rule is about cost, and this is cheap.** A failed `TailoringRun` must be recorded
because the user waited fifteen seconds and we spent money on their behalf; there is something to
reconstruct and something to account for. A fetch is bounded at ten seconds and costs nothing, and
the user is still sitting in front of the form that produced it.

**d. FR-2 says so.** "If scraping fails … the UI shall display a flash error message prompting
manual copy-paste input." The requirement's answer is a message and a path forward, not a record.

**The consequence to state plainly: `JobPostingFetchFailed` propagates *through* the use case.** It is
not caught and converted the way `UploadBaseCv` catches `CvExtractionFailed`. That line in
`CaptureJobPosting` carries a comment naming the contrast and pointing here, and an application test
asserts the propagation — so "fixing" it into a recorded state turns a test red instead of quietly
changing what a `JobPosting` means.

### 3. What is kept from 1.1, unchanged, because consistency is also the point

This ADR breaks one pattern. It is worth being explicit that it breaks exactly one, so the break
reads as a decision rather than as a second dialect:

the cookie asymmetry (POST mints a session, GET answers 401); authorization checked **in the use
case**, never in a router; "not mine" collapsing to 404 rather than 403; events carrying ids and
value objects only; the `except Exception` floor with `from None`; the soft cross-aggregate cap
living in the use case with a comment saying why it is not on the aggregate; the whole-second
`Clock`; imperative mapping and one `TypeDecorator` per value object.

### 4. Two named constructors, not one with optional arguments

`JobPosting.from_pasted_text(...)` and `JobPosting.from_fetched_url(...)`, and **no `__init__`**.

The invariant `source == FETCHED` **iff** `source_url is not None` is then not enforced by a runtime
`if` — there is no third way to build a `JobPosting`, so the pair cannot be made inconsistent. A
single `create(source, url=None, title=None)` would turn a fact about the type into a check some
caller eventually skips.

The same invariant is *also* a database `CHECK` constraint, because a bad backfill or a hand-written
`UPDATE` is not bound by two classmethods.

## Alternatives

- **Two endpoints (`/job-postings/pasted`, `/job-postings/fetched`).** Considered seriously; see the
  counter-argument recorded under decision 1. Rejected because one resource with one authorization
  rule and one cap is worth more than a narrower per-route error map.
- **One endpoint with a flat, all-optional body (`{text?, url?}`).** Rejected: it makes
  `{"text": ..., "url": ...}` and `{}` representable, and both then need a hand-written validation
  branch. The tagged union makes them unrepresentable at the schema, which is the same move the two
  named constructors make in the domain — the two boundaries agreeing on shape is a feature.
- **Recording a failed fetch as a row with `status: fetch_failed`.** Rejected on reason (a): there is
  no artifact to own. It would also change 1.3's assumption that a `JobPosting` always has text, and
  would add a `status` field to the aggregate, the table, the response schema and the React surface
  in exchange for a fact the log line already carries.
- **Recording a failed fetch as a domain event with no aggregate.** Tempting and nearly right — the
  failure genuinely *is* an event. Rejected because events in this codebase are recorded by
  aggregates and released after a save, and there is no aggregate and no save here. The failure is a
  log line at the adapter and a response at the boundary, which is where a caller can act on it.
- **Deduplicating on `source_url`.** Rejected: a URL is an *input*, not the address of something we
  hold (contrast `intake_base_cv.file_key`, which is unique for exactly that reason). A page changes
  between two fetches, two guests may legitimately capture the same posting, and 2.3's history wants
  both.
- **Allowing a `JobPosting` to be edited in this slice.** Rejected as scope: editing arrives in 1.4
  as a named method with its own event. The aggregate is immutable here, and invariant J-3 exists to
  say so, so that nobody adds a bare setter in the meantime.

## Consequences

- **`JobPosting` has no `status` field, and that absence is load-bearing.** A `JobPosting` that
  exists is complete. Slices 1.3 and 1.4 may rely on it.
- **The API returns the posting text**, unlike 1.1, which returns only a character count. A CV is
  dense PII the browser did not need; a job posting is content the user is about to read and, in 1.4,
  edit. Two guards ride along with that: the **list** endpoint returns a 280-character preview and a
  count rather than the full text of every posting, and both `GET`s answer `Cache-Control: no-store`.
- **The `posting` context gets a cascading FK to `identity_guest_session` and an index on it, in this
  slice.** The 1.6 purge predicate stays `expires_at < now()` on the session and nothing else — this
  slice adds a table, not a rule — and the cascade test is written here rather than deferred to the
  slice that will depend on it.
- **Nothing is written outside the database.** No file, no cache entry, no queue row — so unlike 1.1
  there is no crash window between two systems, and no orphan for 1.6's directory sweep to find from
  this slice. The absence gets a sentence in `CaptureJobPosting`'s docstring, because a reader
  comparing it to `UploadBaseCv` will look for the long crash-window comment and should find out why
  there isn't one rather than assume it was forgotten.
- **Phase 2.2 stays additive.** A `user_id UUID NULL` column plus relaxing `guest_session_id` to
  nullable; a posting with no guest session is untouchable by the guest purge (ADR-0006 §3).
- **The failure enum is not persisted.** `FetchFailureReason` exists so the router's reason →
  status/`code` mapping has a closed set to be exhaustive over, and so a log line has a stable
  `failure_reason` value. There is no row to persist it on — which is this ADR, restated as a type.
