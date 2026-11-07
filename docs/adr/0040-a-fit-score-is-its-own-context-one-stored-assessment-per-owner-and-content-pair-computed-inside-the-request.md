# ADR-0040: A fit score is its own context: one stored assessment per owner and content pair, computed inside the request

- **Status:** Accepted
- **Date:** 2026-10-30
- **Relates to:** ADR-0014 (**amended** the same day: this is a stated exception to its rule for what
  goes on a queue, and it does not overturn tailoring's own decision), ADR-0041 (the spend ledger that
  is this design's receipt), ADR-0004 (**amended**: a second port and a second model), ADR-0006
  (**amended (h)**), ADR-0022 (**amended (e)**), ADR-0025 (**amended**), ADR-0035 (**amended**),
  ADR-0013 (the no-row shape), ADR-0029 (the precedent for a small context and for declining the
  foreign key), ADR-0007, ADR-0008 (f)(g) (the transfer set is unchanged), ADR-0031 (a 429's hold).
  ADR-0005 is not amended: its rule is the cost of the work, and this work costs no CPU. Supersedes
  nothing. The number 0039 is held for a backups decision that has not landed, so its absence on disk
  is intended.

## Context

Slice 5.1 gives a person with a CV and a job posting an estimate of how well they fit: a whole
percentage and up to three requirements the CV does not show. It comes from a cheap model, costs a
fraction of a cent, and is open to guests, so a stranger's click spends money. Five forces meet:

1. **It is not a tailoring run.** The Constitution gives `tailoring` "the LLM run and its output
   documents", and a score is neither. It also has to judge a tailored CV later, in a slice that adds
   a re-score, so a port that takes a base CV's `ExtractedText` would need a second method or a lie.
2. **The same CV and posting must always show the same number.** A model will not promise that, and a
   person who sees 62, reloads and sees 58 trusts neither.
3. **ADR-0014's rule reads as if it applies:** *queue the work when its result must outlive the
   request*. A paid score's result would, if asked.
4. **A slow call must not hold a database connection.** Each API process pools ten at most, so one
   held across the call would take the whole site down at about ten concurrent scores.
5. **The gaps are new stored PII.** They are derived from a CV, and they have to be covered everywhere
   a row can be deleted or change owner.

## Decision

**1. A new context, `scoring`, with one aggregate, `FitScore`.** `domain/scoring` imports
`domain/shared` and `domain/identity` (for `Owner`) and nothing else, as `tracking` does. Because it
cannot import `intake` or `posting`, its port takes text types of its own:

```python
class FitScorerPort(Protocol):
    async def assess(self, cv: CvForScoring, posting: PostingForScoring) -> AssessedFit: ...
```

Nothing in that signature knows a model, a retry, JSON or a request. Both value objects normalize
whitespace, so a later re-score of a tailored CV reaches the same method and the same prompt, whether
that slice calls it from a worker or not. `application/scoring` composes `GetBaseCv` and
`GetJobPosting`, so the ownership rule is inherited rather than restated.

**2. Only a success is a row.** Nothing exists to represent before the call, and a failure has an
audience on the socket. So there is no status, no state machine, no `failure_reason` column, no
`version`, no `queued`, no stale sweep. `FitScore` has one named constructor and no mutator, and the
only `UPDATE` that ever touches the table is the claim's, on its two owner columns. This is ADR-0013's
shape. A failure is an HTTP answer, a log line and a ledger figure (ADR-0041). What that gives up is
memory of a refusal: a reload offers the button again, and a repeat is another paid call, bounded by
the IP limit. Accepted.

**3. Identity is content, per owner.** A pair is two fingerprints, SHA-256 of the whitespace-normalized
text. They are stored on the row and never logged, evented or returned. Re-uploading the same CV finds
the stored score; by row ids it would pay again and could show a different number. The same extracted
text is the same CV, so a PDF and a DOCX of one CV can be two pairs. `base_cv_id` and `job_posting_id`
are stored too, but only so deletion can match on them. A CV and a posting are immutable, so a stale
score cannot exist by construction.

- **A stored-score hit consumes nothing:** no limit, no IP limit, no cap, no Redis. The handler looks
  for one **before** the limiter.
- **The key excludes the model and the prompt version.** Each row records both, and a later change to
  either never re-scores or alters a row.

**4. The call is awaited inside `POST`.** No Celery task, queue, polling, beat entry or container. A
lost score costs a fraction of a cent and one click, reproduces exactly that way, and its money is in
the ledger. That is the qualifier ADR-0014's amendment states. A tailoring run costs about a cent and
twelve seconds and yields a document someone edits, so it stays where it is.

**Revisit trigger: the measured cost per score above $0.005, or a complaint about a lost score.** The
port makes that move cheap: a worker task binding the same adapter, with the same judgement.

Timeouts are layered: 8 s per attempt, at most two attempts, one retry of what is worth retrying, and a
hard deadline of 14 s, inside the 15-second budget. A second attempt gets what is left. The request
stays safe by these rules:

```text
 tx A: credential → resolve pair → stored hit? → IP limiter → count → reserve  ── COMMIT
 no transaction, no lock, no connection:  await scorer.assess(cv, posting)     ← up to 14 s
 tx B: store (savepoint) → settle                                              ── COMMIT
```

- **Nothing is held across the call.** The credential's read has already begun a transaction on the
  request's session, so the reservation's commit is what ends it. `application/` names no commit
  (ADR-0002): the adapter behind the ledger port commits, the use case's call order is the boundary,
  and a test asserts `engine.pool.checkedout() == 0` during a suspended `assess`.
- **A client disconnect does not cancel the handler** on today's stack: every middleware is pure ASGI.
  It runs on, stores, settles and sends to nobody, and the next page load shows the score. This is a
  property of the stack, not a promise of the framework, so a test holds it against a real uvicorn and
  goes red the day someone adds a `BaseHTTPMiddleware`.
- **A shutdown does cancel.** `CancelledError` passes the adapter's `except Exception` floor. No row is
  written, the session closes, and the reservation stands as spend (ADR-0041). There is no `shield` and
  no `finally` settle: delaying a shutdown to refund a fraction of a cent is the wrong trade.
- **The event loop stays free.** The model call is awaited network I/O. Normalizing and fingerprinting
  (a CV may be 250,000 characters, on every `POST` and `GET`) and parsing the response go through
  `asyncio.to_thread`. The SDK client is built in the lifespan, because building an SSL context on the
  first request would block every other user.

**5. A second model, and a parser that does not trust it.** `fit_score_model` is configured apart from
`gemini_model`, with `temperature=0`, a fixed seed and thinking off. The SDK calls the seed "best
effort", so **stability is measured, not assumed**: before release, repeated scoring of the evaluation
pairs must vary by at most 5 points, and a CV's matched posting must score at least 20 points above a
mismatched one, since a model answering 70 every time would pass the first test. Below 90, at least one
gap is required; zero is accepted only at 90 or above; more than three is invalid, never truncated.
**Each gap must quote the posting**, and a quote that is not a normalized substring of the posting
refuses the whole answer. That catches the wrong-but-parses case a person would act on: an invented
requirement. The quote is never stored and never leaves the adapter. Nothing checks that a gap is truly
absent from the CV: "Kubernetes" is easy, "led a team" is not, and a half-working check is false comfort.

**6. Two tabs on one new pair cost two calls and leave one row.** Nothing prevents two calls in flight,
since a row does not exist until the end. Each settles normally. Safety is at the store:
`INSERT … ON CONFLICT DO NOTHING` over two unique constraints, one per owner column. The loser waits for
the winner's commit, inserts nothing, re-reads with a fresh snapshot and answers **200 with the winner's
row**, so both tabs show one number. This is not the unique partial index ADR-0014 §4 rejected: that one
would have enforced *one active run* on the hot path, and this one states *one assessment per pair*.

**7. The claim moves scores, with two exceptions** (ADR-0025's amendment). A score on a working copy
never moves. A score whose pair the user already holds never moves either: the user's stays, so a claim
can never replace a number someone has seen. Both leftovers go with the session's cascade.

**8. No foreign key to the CV or the posting.** A cascade would be the right meaning here, since a
score should die with its CV, unlike a run. It would also delete two deletion statements and two locks.
But ADR-0014, 0016, 0023 and 0029 each decline cross-context keys, 0029 declines exactly this one for
"simpler deletion", and nothing forces the contradiction. So the store copies the pattern of ADR-0023
and ADR-0029 §6, inside a savepoint: `INSERT`, then `FOR KEY SHARE` on the CV row and the posting row,
rolling back if either is gone. The deleter issues its score `DELETE` as a **separate statement** after
its own `DELETE`, so a fresh snapshot sees a store that committed during the wait. Whichever goes first,
no score outlives its CV. The savepoint is why tx B still settles after a refused store.

## Alternatives

- **A worker and polling, like tailoring.** This is what ADR-0014's wording asks for, and an earlier
  draft of this slice did it. It needed a state machine, a task, a stale sweep, partial indexes and two
  more claim statements, all to protect a result that costs a fraction of a cent and one click to get
  again. Rejected, with the trigger in decision 4 for when that stops being true.
- **A new aggregate inside `tailoring`.** Cheaper by one package and some small value objects, and the
  real cost of this ADR. Rejected: decision 1, and tailoring's failure vocabulary would gain values its
  `match` must place.
- **Identity by row ids.** Simpler, and it pays again and may show a different number for the same CV.
  Rejected: force 2.
- **A cache across owners.** It would save money on duplicate text, serve one person's gaps to another,
  cross ADR-0022 and make a CV's presence probeable. Rejected.
- **A row for each failure.** It would remember a refusal and give a failure history. It would also
  bring back the status column and grow with the failure rate of a stranger-facing route. Rejected.
- **A foreign key with `ON DELETE CASCADE`.** About three tasks cheaper. Rejected: decision 8. It is
  available by an ADR superseding that convention, if the two locks ever prove costly.
- **An in-flight counter, or a per-process semaphore on calls.** Neither is built. The IP limit and the
  ledger bound the money. A semaphore is one line, if a burst is ever seen rate-limiting tailoring on the
  shared API key.

## Consequences

- **The limits are softer than a worker's would be.** In-flight calls are not rows, so concurrent calls
  on different pairs can overshoot a guest's three. Counting stored rows also means deleting a CV hands
  its slots back. A deliberate script is stopped by the IP limit and by the ledger.
- **A CV sits in a local of the API process** for up to 14 s, not a worker's. `repr=False` on the value
  objects, `include_local_variables=False` in Sentry and `from None` on the adapter's raises are what
  keep it out of a report, and a sentinel test covers the log output.
- **A disconnect's safety depends on the middleware stack**, and a test says so loudly. If that property
  goes, a disconnect becomes a cancellation: not a corruption, just a lost paid result.
- **The API process now waits on the provider.** Tailoring has a worker in front of the same API key, and
  scoring does not. Per-model quotas are unverified, and a burst can surface as `llm_rate_limited` on
  tailoring.
- **Prompt injection is acknowledged, not solved.** A CV or a posting can steer the number. The blast
  radius is what is bounded: the number unlocks nothing and reaches no third party, and a gap cannot
  carry a link or an address, cannot invent a requirement and cannot execute. The UI labels the number an
  estimate. **Any later slice that puts this number beside something with more weight must re-read this
  paragraph.**
- **The next slice to store a re-score** needs an expand migration, since a re-score has no `base_cv_id`.
  It compares `(model, prompt_version)` and re-scores the "before" if they differ. Markdown markers stay
  in a tailored CV's text, and that slice must measure whether they move the score.

## Amendment: 2026-11-07, from the plan of slice 5.2 (`credits-and-demand-test`)

Slice 5.2 re-scores a tailored CV, as the last consequence above foresaw. Decisions 1 to 8 stand; the
re-score goes through the same port, prompt, model, parser and ledger, inline, as decision 4 says. Five
things are added.

- **A draft score.** A run's tailored CV is scored **as the model drafted it** (`run.documents.cv`, never
  the user's revision: ADR-0015 keeps the draft), converted to **plain text** by a new port,
  `DraftTextPort`, whose adapter is the existing TXT renderer in a thread. Plain text and not Markdown,
  because the "before" is flat extracted text and *judged the same way* includes the shape of the input:
  stripping the markers is deterministic and removes the question the consequence above asked this slice
  to measure. It is stored as an ordinary `FitScore` with `base_cv_id` set to the run's base CV (so
  deleting the CV takes it, and the store's CV lock applies) and a new nullable `tailoring_run_id`
  (`draft_of`). Identity stays content: a draft's fingerprint differs from the CV's, so the two never
  collide. The store also takes `FOR KEY SHARE` on the run row and refuses if the run went during the
  call. An edited CV is not scored.
- **The comparison is domain.** `ScoreComparison.between(before, after)` gives `delta = after − before`
  and a verdict: `higher` above +5, `lower` below −5, else `about_the_same`. Five is decision 5's
  stability bar. It never promises a gain, and the copy never says *boost*, *raise*, *improve your score*
  or *guarantee*. The draft is measured for the same stability before release: five scorings of each eval
  draft vary by at most 5 points.
- **Comparisons skip the per-user and per-IP limits.** The credit paid for them. A
  `RequestFitScoreCommand.purpose` of `comparison` skips the owner limit and the `fit_scores` counter, and
  its handler skips the IP limiter. The daily cap and the ledger (ADR-0041) apply as before, so the answer
  at the cap is still 503 `scoring_busy`. Two pairs per run at most, both stored hits after the first call.
- **No re-score of an old "before".** The consequence above said this slice would re-score the "before"
  when `(model, prompt_version)` differ. It cannot without a second row per pair, which decision 3's
  uniqueness forbids, and rows never change. So two scores from different scorers are shown side by side
  as `comparable: false`, with no comparison. Rare until a prompt changes. This is a stated deviation.
- **Prompt injection, re-read as the paragraph above asks.** The draft score now sits beside something
  paid for. Its blast radius stays bounded: **it moves no credit**, refunds nothing, unlocks nothing,
  reaches no third party, and is labelled an estimate, with *about the same* covering a ±5 band. A CV or a
  posting can still steer it. Whether different shapes of the same content score alike is not measured,
  because there is no ground truth for it.

A history entry's deletion now also deletes its run's draft scores, as a separate statement after the
run's `DELETE` (decision 8's pattern), whether or not its posting goes too. That is one more trigger
beside ADR-0006 (h)'s table; erasure and the CV's deletion reach draft scores unchanged, since they
carry the same owner and `base_cv_id`. Draft scores are account data only: the comparison routes are
under `/api/me`.
