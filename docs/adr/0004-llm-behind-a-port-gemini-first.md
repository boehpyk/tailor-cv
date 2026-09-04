# ADR-0004: The LLM lives behind a port; Gemini is the first adapter

- **Status:** Accepted
- **Date:** 2026-09-04

## Context

The LLM call *is* the product (FR-3). It is also the single most awkward dependency this codebase
will ever have:

- **Non-deterministic.** The same input gives different output. No assertion on exact text is stable.
- **Slow and variable.** The end-to-end budget is 15 seconds (PRD §8) and the provider decides most
  of it.
- **Fallible in unusual ways.** Not just "down": rate-limited, truncated mid-object, refusing,
  returning JSON that parses but is missing a field, returning prose where a schema was demanded.
- **Costly per call**, so tests must never touch it, and an unauthenticated endpoint that reaches it
  is a funded denial-of-wallet.
- **A privacy boundary.** The whole CV goes over this wire (Constitution §8).

Two adjacent dependencies have the same shape at a smaller scale: CV text extraction (a "PDF" is
whatever bytes the user uploaded) and job-posting fetching (a URL is an SSRF vector and job boards
actively block scrapers — PRD §9).

## Decision

**Every one of these crosses a domain-defined port.** Named, in `domain/<context>/ports.py`:

| Port | First adapter | Substitute in tests |
|---|---|---|
| `LlmPort` | Google Gemini API | a fake returning fixture documents, and one that raises each failure mode |
| `CvTextExtractorPort` | `pypdf` / `python-docx` / plain read | a fake, plus real extraction tests over a committed corpus |
| `JobPostingFetcherPort` | `httpx` + `trafilatura` | a fake; the SSRF guard is tested directly |
| `DocumentRendererPort` | WeasyPrint / `python-docx` | a fake for flow tests; real renders in a slow-marked test |

The `LlmPort` interface speaks the **domain's** language, not Gemini's. It takes a base CV's extracted
text and a job posting, and returns a tailored CV and cover letter — or raises a domain-level error
(`LlmUnavailable`, `LlmRefused`, `LlmOutputInvalid`). No `GenerateContentResponse`, no `safety_ratings`
and no provider enum crosses into `application/`.

**Structured output is requested from the model and re-validated on receipt.** "The model was asked
for JSON" is not the same claim as "this is valid JSON with the fields we need", and the gap between
those two claims is where the 2 a.m. bug lives.

**Every call has a timeout and a bounded retry with backoff.** A failed run is a *recorded state* of
`TailoringRun`, never an unhandled exception and never a 500 with nothing on disk. The user must be
able to tell the difference between "still working" and "this failed, try again".

## Alternatives

- **Call the SDK directly from the route.** Fastest to write, and it welds a vendor's response shape,
  retry semantics and exception types into the use case. Rejected — and note that with the port, the
  fake adapter makes the entire failure contract testable, which is the concrete payoff, not the
  theoretical one.
- **LangChain (or a similar framework) as the abstraction.** Rejected: it is someone else's
  abstraction over a boundary we understand well enough to own in fifty lines, and it would import a
  large dependency tree into the layer we most want to keep legible. Revisit only if multi-step
  agentic flows appear.
- **Multiple providers behind the port from day one.** Rejected as speculative (Constitution §5). The
  port makes the second provider cheap *when there is a reason*; building it now is paying for
  flexibility we cannot yet aim.
- **Recording real API responses as fixtures (VCR-style).** Attractive and worth revisiting for a
  small number of golden-path tests. Not the default, because a recorded cassette silently becomes a
  claim about a model version that no longer exists.

## Consequences

- **The suite never calls the real API.** CI has no `GEMINI_API_KEY`. Prompt quality is therefore
  *not* covered by tests — it is evaluated by hand against a committed corpus of real postings
  (PRD §10 validation 2, `make eval`). Be honest about this: green tests mean the plumbing works.
- The prompt is a first-class artifact — versioned, in one place, reviewed like code. A prompt edit is
  a behaviour change and gets a commit message that says so.
- Latency instrumentation belongs in the adapter: record the call duration and token counts on every
  run. Without it, the 15-second budget cannot be defended and every complaint is anecdote.
- **Never log prompt or completion bodies.** They contain the user's CV (Constitution §8). Log ids,
  durations, token counts, outcome.
- Swapping providers is one adapter and one binding. That claim is only true if nothing outside the
  adapter ever imports the SDK — which import-linter checks for `domain`, and code review must check
  for `application`.
