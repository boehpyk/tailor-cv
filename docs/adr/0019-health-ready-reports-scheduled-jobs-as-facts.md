# ADR-0019: `/health/ready` reports scheduled jobs as facts, not as readiness

- **Status:** Accepted
- **Date:** 2026-09-19

## Context

`/health/ready` has answered exactly one question since Phase 0: **can this process serve a request
right now?** It probes Postgres, Redis and Celery, and returns 503 when any of them says no. That
shape is load-bearing beyond the dashboard — the deploy's release check polls this endpoint and will
not promote a container that never goes green, and Docker and Traefik read it too.

Slice 1.6 adds a job whose only failure mode is **silence**. Celery Beat stopping raises nothing and
logs nothing, and the existing Celery probe structurally cannot see it: `control.ping` reaches
*workers*, and beat is not a worker. A guest purge that quietly never runs does not make the API
unable to serve — it breaks a privacy promise, which is worse in a different way and on a different
timescale.

So the slice needs somewhere to publish two facts (*when did the purge last run?* and *how much is
overdue right now?*) on an endpoint an operator and the browser already poll. The tempting home is
the readiness report, and the tempting implementation is to let a stalled purge turn it red.

There is a second, quieter force. The purge's schedule ships **off** (ADR-0018 §5) and is turned on
only after a hand rehearsal on real data. During that window the job is *correctly* not running, and
any staleness alarm computed over it would be on for the whole period.

## Decision

**1. `/health/ready` gains a `jobs` object, and nothing in it can change the status code or `ready`.**
`ready` stays `all(dependency.healthy)`. A stale purge is **200** with `stale: true`. Readiness is
about serving a request; a scheduled job is reported as a **fact**.

**2. The probe returns a different type, so the mistake is unrepresentable rather than merely
avoided.** `probe_guest_purge` returns a `JobStatus`, not a `ProbeResult`. Reusing `ProbeResult`
would put the new probe one careless `all(r.healthy for r in results)` away from 503-ing the
application over background hygiene — and that line already exists in the handler. A different type
cannot be swept into it.

**3. The `jobs.guest_purge` shape is fixed and pinned by a contract test:**

```jsonc
"guest_purge": {
  "scheduled": false,                  // is the beat entry registered in this deployment?
  "last_run": "2026-09-19T16:00:00Z",  // RFC 3339, UTC, whole-second; null if never / Redis flushed
  "last_run_age_seconds": 3612,        // null when last_run is null
  "last_outcome": "ok",                // "ok" | "failed" | null
  "overdue": 137,                      // expired sessions right now; null if the count failed
  "stale": false,                      // scheduled && (last_run is null || age > 3h)
  "detail": null                       // only set when a field could not be computed
}
```

**4. `scheduled: false` ⇒ `stale: false`.** Staleness is a judgement about a schedule that is
supposed to be firing. An alarm that is on for the entire pre-rehearsal window is an alarm nobody
reads, and by the time it matters nobody will see it change. `scheduled: false` **is** the honest
signal in that window, and the UI renders it in plain words rather than as a colour.

**5. A failed sub-computation degrades the field, never the response.** If the backlog count times
out, `overdue` is `null` with a `detail` and the response stays 200 — unless the Postgres probe is
already answering 503 for its own reason, which it will be if Postgres is genuinely down.

**6. This sets the rule for every future scheduled job.** The two existing beat sweeps (stale
tailoring runs, stale export jobs) have no heartbeat at all today; when they get one, it goes under
`jobs`, with the same 200-with-a-fact contract. A new `jobs` member is never a new way to fail
readiness.

## Alternatives

- **503 on a stale purge.** The honest-looking option, and wrong: it would pull a perfectly
  serving application out of load balancing, fail the deploy's readiness check, and restart
  containers — over background hygiene that the restart does not fix. It also inverts the
  relationship between the two failures: the API being unable to serve is minutes-scale and
  self-announcing; a purge not running is hours-scale and silent, and needs to be *read*, not to
  take the site down.
- **A separate `/health/jobs` endpoint.** Rejected: an operator at 2 a.m. should not have to know a
  second URL exists, and the browser already polls this one every 15 s. A fact nobody fetches is a
  fact nobody has.
- **Reuse `ProbeResult` with a `blocking: bool` flag.** Rejected: it keeps the wrong type in the
  wrong list and moves the safety into a boolean somebody has to remember to read. See decision 2.
- **Always compute staleness, even when the schedule is off.** Rejected for the reason in decision 4.
  If this is ever revisited, the runbook's `stale: true` row is amended at the same time, and the UI
  copy with it — the two must not disagree.
- **Put the last-run fact in Postgres rather than Redis, so it survives a flush.** Rejected with the
  aggregate in ADR-0018: a run row is exactly the thing a broken job can still write. A flushed Redis
  shows `last_run: null`, which the runbook already tells the operator to investigate, and `overdue`
  — the signal that cannot be faked — is read from Postgres anyway.
- **Authenticate the endpoint now that it publishes an operational count.** Not in this slice.
  `overdue` is a single integer carrying no identifier and no PII, and the endpoint is the one Docker
  and Traefik probe unauthenticated. Recorded so the decision is a decision.

## Consequences

- **A green `/health/ready` no longer means "everything is fine".** It means "this process can serve
  a request". Reading `jobs.guest_purge` is a separate act, and the runbook and the UI both say so.
- **`/health/ready` now runs a `COUNT` on every poll.** It is an index range scan over
  `ix_identity_guest_session_expires_at`, bounded by its own timeout and measured at `/verify`. If it
  ever exceeds 20 ms p95 the count is capped (`LIMIT 10001` plus an `overdue_capped` flag) — a
  decision already made rather than a surprise at 2 a.m.
- **The browser now renders an operational fact.** The retention block in `SystemStatus` shows
  counts, an age and a status word — no session id, no filename, no storage key, no path — and a grep
  test enforces it.
- **The client type makes `jobs` optional.** During a deploy the browser can be served a bundle newer
  than the API; an older API with no `jobs` member must render "not reported", not crash.
- **`last_run` can go stale while the job is healthy.** A heartbeat write that fails after a
  successful purge leaves the old instant in place. That is why the runbook's first row is the
  **backlog**, not the heartbeat: `overdue` falling is the fact, `last_run` is the convenience.

## Amendment: 2026-10-02, from the plan of slice 2.5 (`identity-email-verification`)

Slice 2.5 adds the second scheduled job decision 6 anticipated — retention's identity token sweep
(ADR-0018's amendment (b)). Decisions 1–6 stand.

**(a) `jobs.identity_token_sweep` reports the backlog, and only the backlog.**

```jsonc
"identity_token_sweep": {
  "scheduled": true,   // the beat entry is unconditional (it ships on); reported so jobs read alike
  "overdue": 0         // pending registrations + password resets + logins with expires_at < now - 2h;
                       // null (with a "detail") when the count could not be computed
}
```

- **`overdue` counts rows expired more than two intervals ago.** A healthy hourly sweep leaves rows at
  most one interval past their expiry, so it reads 0; a positive number means at least two ticks did
  not run or did not finish. That is decision 4's lesson from the purge: the backlog cannot be faked by
  a job that is not working.
- **No `last_run`, `last_outcome` or `stale`.** Decision 6 said a job gets those *when it gets a
  heartbeat*, and this one has none: it deletes rows nobody can use, so a missed tick costs nothing a
  user notices and the backlog is a sufficient fact. A heartbeat can be added later under the same
  contract without changing the meaning of `overdue`.
- **It never changes the status code or `ready`** (decisions 1 and 2): the probe returns a
  `JobStatus`-shaped fact, not a `ProbeResult`. A failed count degrades the field (decision 5).
- **Cost.** Three index range scans on the three tables' `expires_at` indexes, bounded by the probe's
  own timeout; measured at `/verify` against the same 20 ms p95 the purge's count was held to.
- **The browser's status panel is not changed.** The field is operator JSON. The client type names
  only `guest_purge`, optionally, so a bundle that does not know the new member ignores it, and an
  older API that lacks it breaks nothing.
