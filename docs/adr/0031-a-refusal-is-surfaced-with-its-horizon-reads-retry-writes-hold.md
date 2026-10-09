# ADR-0031: A refusal is surfaced with its horizon; reads retry, writes hold

- **Status:** Accepted
- **Date:** 2026-10-09
- **Relates to:** ADR-0014 (a run that spends money is always a row; §6's one retry mechanism, in the
  adapter — **amended** the same day: a run carries `retry_not_before`), ADR-0016 (an export is a
  job), ADR-0015 (the edit is a revision guarded by `version` — the reason autosave may retry),
  ADR-0008 / ADR-0021 (the auth limiters, failing closed), ADR-0004 (the LLM behind a port — **not
  re-opened**). Supersedes nothing.

## Context

Roadmap 3.3 asks for *"rate-limit / retry UI feedback with exponential backoff surfaced to the
user"*. Reading the code before deciding what that means found **four** retry-shaped mechanisms, not
one, and none of them is visible:

1. **Our own limiters.** Nine places answer 429 `rate_limited` with a `Retry-After` header. The
   windows are fixed and hourly, so the honest wait is anywhere from 1 to 3600 seconds. One control
   (the guest-work claim) waits it out; four surfaces say *"a few minutes"* (wrong by up to an hour);
   the job-posting panel relays the server's prose (*"2537 seconds"*), the one place the client reads
   a message instead of a code.
2. **The worker's LLM retry** (ADR-0014 §6): two attempts, a fixed 1 s apart, under a 25 s deadline.
   Not exponential, and over before a 1 s poll can show it.
3. **The client's read retries.** TanStack retries the run and export polls at 1, 2 and 4 s, then the
   UI says *lost contact*; while the browser is offline it pauses. This is the only exponential
   backoff in the system, and the user never sees it working.
4. **Gemini being busy.** A run that failed `llm_rate_limited` offers *Try again* at once, with the
   hint *"Give it a minute"* and nothing enforcing it. PRD §6 asks for a backoff prompt here.

The force that decides everything below is the product's oldest rule: **a user who cannot tell
"still working" from "failed" refreshes, and pays for a second LLM call.** Feedback about retrying
must make that distinction sharper. A client that quietly retries a paid write blurs it exactly where
it costs money.

The learning force (FR-7): where does a deadline learned from one response live in React? The
tempting answers — a global store, the query cache, an effect copying the error into state — are each
the wrong idiom for a different reason, which makes this a good place to show the right one.

## Decision

1. **`Retry-After` is the backoff contract for every 429.** Status 429, code `rate_limited`, header
   `Retry-After: <seconds>`. The client reads the header and the code, **never the message**. A
   structural test over the routers pins that every 429 carries the header, so a new route cannot
   forget it. The server's limits and windows are unchanged; the wait is fixed, not exponential, and
   the UI says so rather than dressing it up.
2. **Writes hold; they never retry by themselves.** After a 429, the refused control is disabled
   until the deadline and says when it comes back: a clock time (*"at 14:00"*) beyond 90 s, *"in N
   seconds"* at or under. When the hold ends the control re-enables silently (a `role="status"`
   line, no focus move) and the user clicks. This covers starting a run, *Try again*, an export
   request, both uploads, the posting submit, the claim, login, register and the reset requests.
   **The one named exception is autosave**, which already retries after `Retry-After` on its own
   timer: its write carries the document's `version` (ADR-0015), so a repeat can be refused but never
   applied twice, and it spends nothing.
3. **Reads retry with backoff, and the UI says so.** The pollers keep TanStack's exponential delay
   unchanged; the run and export screens show *"trying again (attempt n of 4)"* and *"you're
   offline"*, derived from the query's own `failureCount` / `fetchStatus`. The backoff shown is the
   backoff that runs, because nothing wraps it.
4. **After Gemini says busy, *Try again* holds for 60 s.** The API computes the instant
   (`retry_not_before`, ADR-0014's amendment of the same date) beside `retryable`, for the same
   reason `retryable` is on the wire: a TypeScript copy of a rule is a second authority. It is
   advisory; the hourly limiter still enforces.
5. **A hold is component state, derived from the response that caused it.** One hook keeps
   `(error, receivedAt)`, updated during render when the error changes, and derives the deadline;
   its only effect is the clock tick. No global rate-limit store, no query-cache entry, no browser
   storage.
6. **A 503 holds nothing.** It says nothing about when to come back; the existing *Retry* stays.

## Alternatives

- **Auto-retry a refused write after `Retry-After`.** The tempting one: it is what "exponential
  backoff" usually means in a client library. Rejected because the write is a paid call or a
  credential, a retry fired from an abandoned tab spends money the user no longer intends to spend,
  and a silent re-submit is exactly the "still working or failed?" blur the product forbids.
- **Show the worker's LLM attempts** ("retrying, attempt 2 of 2"). Rejected: the retry is over in
  about one poll frame, and showing it would either teach `LlmPort` about attempts or need a
  migration to record them. Cost far above the information.
- **An exponential cooldown after repeated `llm_rate_limited`** (60 s, 120 s, 240 s…). Rejected for
  now: counting a streak is a query over the owner's recent runs, for a provider condition we have
  not seen often. Revisit if `llm_rate_limited` exceeds about 5 % of runs.
- **Quota headers** (`RateLimit-Remaining` and friends) so the UI can warn before the refusal.
  Rejected: a change to all nine 429 sites and the limiter's API, for a warning nobody has asked for.
- **A shared client-side rate-limit store**, so one 429 holds every control that shares the budget.
  Rejected: the client does not know the server's namespaces or scopes, so it would guess — a second
  authority that drifts. One extra refused request per control costs nothing.
- **Persisting holds across a reload** (browser storage). Rejected: it would be the client's
  second, longer-lived copy of a server fact, and a reload costs one free 429 to relearn the
  deadline.
- **Refusing a run inside `retry_not_before` on the server.** Rejected: a domain rule needing the
  owner's last run, racy across tabs, for a 60 s nudge the limiter already backstops.

## Consequences

- **Easy:** every refusal tells the user *when*, every poll failure tells them *still trying*, and
  "failed" stays reserved for things that failed. A new 429 route is caught by the structural test; a
  new re-clickable control has one hook to call.
- **Hard / honest:** with hourly windows, the truthful message is often *"try again at 15:00"*, up to
  59 minutes away where the old copy said *"a few minutes"*. That may draw complaints the old wording
  did not. The fix is a decision about the limits, not about the UI.
- **Holds are per control and per page load.** Two tabs each learn of a 429 by sending one; a reload
  forgets a hold. Accepted: nothing is spent.
- **No client jitter.** If concurrent users reach the hundreds, every tab's polls retry at 1/2/4 s
  after an outage together. Add a jittered `retryDelay` (injectable, for tests) then.
- **Watch:** the share of runs failing `llm_rate_limited` (the trigger for the exponential cooldown),
  and complaints about long waits (the trigger for revisiting the limits).
