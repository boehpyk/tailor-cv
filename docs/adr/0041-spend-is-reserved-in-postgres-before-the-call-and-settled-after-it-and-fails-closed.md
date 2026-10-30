# ADR-0041: Spend is reserved in Postgres before the call and settled after it, and fails closed

- **Status:** Accepted
- **Date:** 2026-10-30
- **Relates to:** ADR-0040 (the fit score whose calls this pays for; it computes them inside the
  request), ADR-0004 (a costly call an unauthenticated route can reach is a funded denial-of-wallet),
  ADR-0014 §2 and its amendment (the money must be recorded even with no row), ADR-0031 §6 (a 503 holds
  nothing), Constitution §7 and §8. Supersedes nothing.

## Context

Fit scoring (ADR-0040) lets a stranger spend our money with a click. Rate limits bound one IP, not the
sum of all of them, so the operator needs a hard ceiling on the day's total. Four facts shape it:

1. **The cap must hold under concurrency.** A check followed by a later write lets N requests pass the
   same check, and they are all in flight for seconds. Nothing commits a cost until the call ends, so
   checking what has been spent so far is a check of the wrong number.
2. **The call can fail, time out or be killed.** `LlmCallMetrics` describes only the surviving attempt,
   and a timed-out attempt reports no usage although it may have been billed. A cap built on it would
   undercount exactly the calls that go wrong.
3. **Failures are not rows, and the rows that exist get purged**, so the total cannot be derived from
   `scoring_fit_score`.
4. **The request holds nothing across the call** (ADR-0040 decision 4), so whatever records the money
   must be durable before the call starts, and must survive never being told how the call ended.

## Decision

**1. One row per UTC day, in Postgres, in whole micro-USD.**

```text
scoring_daily_spend(day DATE PK, spent_microusd BIGINT NOT NULL CHECK (>= 0), updated_at)
```

Integers, rounded up, so the money never rounds in our favour:
`ceil(input_tokens × input_price / 1e6) + ceil(output_tokens × output_price / 1e6)`, where
`output_tokens` is candidates plus thoughts, since thoughts bill at the output rate. "Daily" is UTC, and
the page never says so. The row holds no personal data and survives every deletion, which is what makes
the cap survive the purge.

**2. Reservation: one atomic statement, committed before the call.**

```sql
INSERT INTO scoring_daily_spend … VALUES (:day, :reserved, :now)
ON CONFLICT (day) DO UPDATE SET spent_microusd = scoring_daily_spend.spent_microusd + :reserved, …
WHERE scoring_daily_spend.spent_microusd + :reserved <= :cap
RETURNING spent_microusd;
```

No row returned is a refusal, so concurrency cannot overshoot: each request's check and write are one
statement under the row's lock. The amount is a worst case over the actual inputs:
`attempts × (ceil((cv_chars + posting_chars + overhead) × input_price / 1e6) + ceil(max_output_tokens ×
output_price / 1e6))`. That takes one token to be at most one character, and thinking to be off. Both
are assumptions, and both are measured before release: a higher ratio, or a billed thought, breaks the
upper bound and must be fixed first. The reservation sits on the only path to `assess`, and its commit
is also what ends the request's first transaction (ADR-0040 decision 4).

**3. Settlement: reduce the ledger to the actual cost, after the call.**

```sql
UPDATE scoring_daily_spend SET spent_microusd = GREATEST(spent_microusd + :actual - :reserved, 0), …
WHERE day = :day;
```

- **`:day` is the reservation's**, held in a local, so a call across midnight adjusts the right row.
- **Accounting is per attempt.** Each attempt adds its usage. An attempt with none (a timeout, a missing
  usage block on a 200) counts at its worst case, and the total rides on the success **and** on the
  exception, because usage unknown is not usage zero.
- **A success settles in the same transaction as its row**, so a score and its cost commit together. A
  failure settles on its own. A call that made no provider request (inputs too large, no API key) settles
  to zero.
- **If settling a failure fails**, it is logged by type, the reservation stands, and the user still gets
  the failure's own answer.

**4. There is no pending state, by design.** A reservation is spend from the moment it commits, and
settlement is a refund of the difference. A process that dies, or a shutdown that cancels, between the
two leaves the day's figure overstated by that one reservation: on estimated prices about a fifth of a
cent typically and a little over a cent at worst, gone at the next UTC midnight. No row, no lock, no
sweep. The true figure is unknowable there, and the error points the safe way.

**5. It fails closed.** No database, no reservation, no call: *fail closed when the cost is money.* The
IP limiter in front of it, on Redis, is built with `fail_open=False` for the same reason. A stored-score
hit touches none of this, so a person's existing score still shows with Redis down or the cap full.

**6. At the cap the answer is 503 `scoring_busy`, with no call.** The word is *busy*, never "out of
money". It holds nothing (ADR-0031 §6). One `scoring.spend_cap_reached` line records the day and no
owner. The same answer applies when this reservation alone exceeds what is left, so a long CV can be
refused while a short one fits. Accepted.

**7. Prices and the cap are settings, and the cap is $2 a UTC day.** `fit_score_daily_spend_cap_usd`
may be `0`, which means *always busy* and is the off switch. Both prices must be `> 0` at startup. **The
cap is only as true as the prices**, so before release they are read from the provider's pricing page on
the day, and the measured cost is checked against the provider's own usage view. On estimated prices,
unverified until then, $2 is several thousand scores: an abuse ceiling, not an operating limit.

## Alternatives

- **A counter in Redis.** Redis is durable here, so survival would not decide it. Atomicity does: a
  success's cost commits with its row only if both are in one database. And the IP limiter already fails
  closed on Redis, so the ledger should not share its failure.
- **Summing the score rows.** The purge deletes them and failures are not rows. Rejected: fact 3.
- **Check, call, then add the actual cost.** Simple, and it overshoots by every call in flight, on the
  day it matters most. Rejected: fact 1.
- **Reserve an average instead of a worst case.** Fewer false refusals near the cap, and the ceiling
  stops being a ceiling. Rejected.
- **A `pending` row per call, with a sweep to expire it.** The accurate version of decision 4: exact
  spend after a crash, for a table, a job and a state to recover, all to correct about a cent until
  midnight. Rejected.
- **`asyncio.shield` or a `finally` settle on cancellation.** It would hold up a shutdown to refund a
  fraction of a cent. Rejected: ADR-0040 decision 4.
- **Fail open while the database is down.** Then a Postgres outage is the moment nothing limits spend.
  Rejected. The request could not store its result anyway.

## Consequences

- **The ledger overstates, on purpose.** Each lost call adds its reservation until the next UTC
  midnight. Nothing reconciles it, and the provider's billing view remains the true figure.
- **A stranger can use up the cap.** Anyone who gets past the IP limit from enough addresses can make
  scoring busy for everyone until midnight. That is what a ceiling means: the worst day is a bounded bill
  and a *busy* message, tailoring and stored scores carry on, and the log line says it happened. A daily
  figure comes with a later slice.
- **A commit in the request's path:** two transactions per new score, so one extra checkout and
  `pool_pre_ping` round trip, held to the budget of 100 ms p95 overhead outside the call.
- **The table grows by one small row a day**, and nothing deletes it. Harmless, and it is the only history
  of spend there is.
- **The cap is a code fact in a public repository.** Knowing it helps nobody beyond what the IP limit
  already allows.
- **The pattern is reusable, but not yet shared.** The next spend-bearing call that wants a ceiling should
  read this first. A generic ledger for one caller would be a port for nobody.
