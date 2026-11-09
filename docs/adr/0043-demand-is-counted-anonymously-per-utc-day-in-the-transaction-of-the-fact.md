# ADR-0043: Demand is counted anonymously, per UTC day, in the transaction of the fact

- **Status:** Accepted
- **Date:** 2026-11-07
- **Relates to:** ADR-0042 (the credits whose runs, clicks and refunds are counted), ADR-0041 (the spend
  ledger: the other table that survives every deletion, and the precedent for a UTC day), ADR-0040 (the
  fit scores counted), ADR-0006 and its amendment (i) (why the counters outlive the purge), ADR-0032 and
  ADR-0035 (the operator reads them in `/admin`, not over SSH), ADR-0002 (a port in `domain/shared`).
  Supersedes nothing.

## Context

Phase 5's gate (D-10) is a demand test: within eight weeks of the first post, 100 fit scores, 20 sign-ups
and 5 strangers clicking a pack. Phase 6 later needs a failure rate of runs. Four facts shape how those
numbers are kept:

1. **The rows that would answer the question are deleted.** Guest scores go with the 24-hour purge, and an
   erased account takes its runs, scores and credits. Counting rows on day 50 counts what survived.
2. **The question is about volume, not people.** Nobody needs to know *who* scored or clicked, and a table
   that knew would be a second copy of behaviour the purge exists to forget.
3. **A count that disagrees with its fact is worse than none.** A score that rolled back, or a refused run,
   must not be counted.
4. **Four contexts produce the facts**: `scoring`, `identity`, `billing` and `tailoring`.

## Decision

**1. One row per UTC day and counter: `(day, counter, count)`, and nothing else.** No user id, session id,
IP, user agent or run id. A catalogue test asserts the table has exactly those three columns and no
id-shaped or owner column. The rows hold no personal data, so they survive the purge and erasure, which is
their purpose. "Day" is the UTC date of the fact's instant, read once from the `Clock` by the use case.

**2. A closed set of seven counters**, a `StrEnum` checked in the database:

| Counter | Incremented by |
|---|---|
| `fit_scores` | a new stored score from a check route; not a stored hit, not a race loser, not a comparison |
| `fit_scores_busy` | a scoring reservation refused at the daily cap |
| `sign_ups` | `ConfirmRegistration`, after the `User` is added |
| `pack_clicks` | every pack click |
| `pack_clickers` | an account's first pack click ever (the gate's "strangers", minus friends the owner knows) |
| `runs_failed` | `RecordRunFailure`, at every failure site |
| `runs_succeeded` | `ExecuteTailoringRun`, before the success save (the failure rate's denominator) |

A new counter is a deliberate change to the enum, the CHECK and this table, never a free-form string.

**3. Incremented in the transaction of the fact.** `DailyCounterPort.increment(counter, day)` lives in
`domain/shared/counters.py`, because four contexts count and none should import another. Its adapter is
one statement on the caller's session,

```sql
INSERT INTO demand_daily_counter (day, counter, count) VALUES (:day, :counter, 1)
ON CONFLICT (day, counter) DO UPDATE SET count = demand_daily_counter.count + 1
```

and it **never commits**: the fact's own commit does. A rolled-back fact counts nothing, and a failed
upsert fails its fact (one transaction, by design). The one fact with no transaction of its own,
`fit_scores_busy`, is committed by its handler before it answers 503. A read port,
`DemandCounterQuery.between(from, to)`, sits beside the write port.

**4. The operator reads them at `/admin/demand`.** `GET /api/admin/demand-counters?from=&to=` returns each
day in the range with zeros filled, and totals. A range over 92 days, or `from > to`, is 422; a non-admin
gets the admin firewall's 404. No CLI: Phase 4's gate was *no SSH*.

## Alternatives

- **Count rows when asked.** No new table, and wrong by fact 1: the purge and erasure delete the evidence.
  Rejected.
- **Count log lines.** Logs already exist, but they are not in the transaction of the fact, they rotate,
  and a log line that carries enough to be counted per day is one step from carrying an owner. Rejected.
- **Distinct guests, IPs or devices.** The gate would like them and they need an identifier. Not counted.

## Consequences

- **The counters are hot rows** on a busy day: one row lock per counter per day, held until each fact's
  commit. At this product's traffic, irrelevant; named so nobody is surprised.
- **Small numbers can be read as one person's action**, on a quiet day, by the operator only, who already
  holds the rows that recorded it.
- **The table grows by at most seven rows a day** and nothing deletes it.
- **The numbers count actions, not intent.** A script that registers accounts counts as sign-ups, and a
  friend's click counts as a pack click; the owner subtracts what he knows of.
