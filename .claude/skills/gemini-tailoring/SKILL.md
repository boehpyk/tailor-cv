---
name: gemini-tailoring
description: How to change anything that touches the LLM in TailorCraft — the prompt structure, structured output and its re-validation, the timeout/retry/failure contract, token budgeting against the 15-second target, the fake adapter every test uses, and how to evaluate prompt quality (which is not a unit test). Use when working on tailoring, prompts, the Gemini adapter, or any LLM failure path.
---

# Working on the LLM path

The tailoring call is the product (FR-3) and the most awkward dependency in the codebase: slow,
non-deterministic, occasionally refusing, priced per call, and carrying the user's entire CV over the
wire. Everything below follows from those five facts.

All access goes through `LlmPort` (ADR-0004). The Gemini adapter lives in
`infrastructure/llm/gemini.py` and **nothing else in the repository imports the SDK.** If a second
file imports it, the port has stopped being a boundary and become a rename.

## The shape of a call

```
use case ──► LlmPort.tailor(cv_text, posting) ──► GeminiAdapter
                                                   ├─ build prompt (one module, versioned)
                                                   ├─ request structured output (a schema)
                                                   ├─ timeout + bounded retry w/ backoff
                                                   ├─ parse AND RE-VALIDATE
                                                   ├─ record duration + token counts
                                                   └─ raise a DOMAIN error on failure
```

## The prompt is a first-class artifact

It lives in one module, it is versioned, and a change to it is a behaviour change that gets a commit
message saying so. Structure it as: role and constraints → the job posting → the base CV → the output
contract. Put the instruction to not invent experience **in the constraints**, and test that it holds
in the eval set — a model that fabricates a job the candidate never had is worse than no product.

Keep the CV and the posting clearly delimited from the instructions. Text arriving from a scraped job
page is untrusted input that ends up inside a prompt; treat prompt injection as a real category here,
not a theoretical one.

## Structured output, then re-validate

Ask the model for a schema. Then **check what came back**, because "we asked for JSON" and "this is
valid JSON with the fields we need" are different claims and the gap between them is where the 2 a.m.
bug lives. A response that parses but is missing the cover letter is `LlmOutputInvalid`, not content.

## The failure contract — write it before the happy path

| Failure | Domain error | User sees |
|---|---|---|
| Timeout / network / 5xx | `LlmUnavailable` | "Couldn't reach the model — try again", run marked failed |
| 429 rate limit | `LlmRateLimited` | retry indicator with backoff (PRD §6) |
| Safety refusal | `LlmRefused` | a specific message; do **not** silently retry a refusal |
| Unparseable or incomplete output | `LlmOutputInvalid` | "Generation failed — try again"; nothing persisted as content |

**A failed run is a recorded state of `TailoringRun`, never an unhandled exception and never a 500
with nothing on disk.** The user must be able to tell "still working" from "this failed", or they
refresh and you pay for a second call.

Retries are **bounded** and only for the retryable classes. Never retry a refusal, never retry an
invalid-output response more than once, and never retry without backoff.

## The 15-second budget (Constitution §7)

The provider owns most of it, which means everything you control has to be small:

- One call, not a chain. A multi-step pipeline multiplies a variable latency by the number of steps.
- Cap what you send. A 12-page CV and a 4,000-word posting is a slow call and a worse result — trim
  the posting to its extracted main content and bound both inputs.
- **Record duration and token counts on every call.** A budget with no measurement is a wish, and
  every latency complaint without it is anecdote.
- Stream if the UI can use partial output; do not stream just because the SDK can.

## Tests never call the real API

CI has no `GEMINI_API_KEY` — deliberately, so a suite cannot silently start spending money if a key
appears. Tests drive fakes:

- `FakeLlm` returning fixture documents (the golden path).
- One fake per failure class, raising `LlmUnavailable`, `LlmRateLimited`, `LlmRefused`,
  `LlmOutputInvalid`. **These are the tests that pay for the port existing.**

## Prompt quality is evaluated, not unit-tested

This is the honest part. A green `pytest` says the plumbing works; it says nothing about whether the
output is any good. Keep a committed corpus of real postings and sample CVs and run `make eval` by
hand against the real API when the prompt changes (PRD §10 validation 2). Read the output. Score it on
the things that matter — does it use the posting's actual requirements, does it keep the candidate's
real history, is the cover letter something a person would send.

Automate what you can cheaply assert (no invented employer names, length in range, both documents
present); accept that the rest is judgement, and do not dress judgement up as a passing test.

## Privacy (Constitution §8)

The CV goes to the provider. That is unavoidable, and it is **stated to the user**, not buried.
Nothing else goes: no email address, no account id, no other user's content in the same prompt.

**Never log a prompt body or a completion.** Log run id, durations, token counts, outcome. A debug
log is the easiest way to leak a stranger's address and phone number into a file nobody thinks of as
a database.

## Cost control

The tailoring endpoint is unauthenticated for guests and costs money per call. **Rate-limit it from
the first slice**, per session and per IP. An unauthenticated endpoint that spends money is a funded
denial-of-wallet, and discovering that from a bill is an expensive way to learn it.
