# ADR-0005: Celery + Redis for exports and scheduled work

- **Status:** Accepted
- **Date:** 2026-09-04

## Context

FR-5 requires PDF and DOCX generation, and PRD §9 explicitly puts heavy conversions on a background
worker queue "to keep the API responsive". Two workloads need to leave the request:

1. **Rendering** — WeasyPrint is CPU-bound and synchronous. Running it inline in an async FastAPI
   route blocks the event loop for every concurrent user, not just the one downloading. This is the
   classic async footgun: it looks fine with one user and collapses with five.
2. **Scheduled sweeps** — the 24-hour guest purge (FR-6) must run whether or not anyone visits.

Redis is already in the stack for caching, sessions and rate limiting.

## Decision

**Celery 5 with a Redis broker and result backend**, run as two containers: `worker` (consumes) and
`beat` (schedules). Enqueuing goes through a domain-defined `TaskQueuePort` — the application layer
asks for work to happen later; it does not import Celery.

- **TXT and Markdown render inline.** They are string manipulation; queuing them would add a round
  trip and a polling loop to save nothing. **PDF and DOCX go to the worker.** The rule is the cost of
  the work, not the tidiness of doing all four the same way.
- An export is an `ExportJob` aggregate with an explicit state (`queued` / `rendering` / `ready` /
  `failed`) that the client polls. The state lives in Postgres, not only in Celery's result backend:
  the result backend is an implementation detail with a TTL, and "where is my file" must survive a
  Redis flush. **(2026-09-17)** That sentence describes the exports that *leave the request* — `pdf`
  and `docx`. `md` and `txt` are representations of the document and leave no row; see ADR-0016 §1.
- The worker runs with `--max-tasks-per-child` so a renderer leak is recycled rather than accumulated.

## Alternatives

- **FastAPI `BackgroundTasks`.** In-process: the work dies with the container, there is no retry, no
  visibility, and no separate scaling. Fine for fire-and-forget logging, wrong for a file the user is
  waiting on. Rejected.
- **A thread pool (`run_in_executor`).** Solves the event-loop blocking and nothing else — no
  scheduling, no durability, no isolation of a memory-hungry renderer from the API process. Rejected
  as the primary mechanism; still the right tool for a short synchronous call inside a request.
- **RQ or Dramatiq.** Simpler than Celery and genuinely tempting. Rejected narrowly: Celery's Beat
  scheduler covers the retention job without a second mechanism, and the ecosystem knowledge
  transfers. This is the decision most likely to be revisited, and the `TaskQueuePort` is what makes
  revisiting it cheap.
- **A separate scheduler process (cron on the host).** Rejected: it lives outside the deploy, so it
  runs whatever code was on the box last, and it is invisible to every check this stack has.

## Consequences

- **Two more containers that run application code**, which means two more entries in the deploy's
  pull-and-verify list (cicd.md). A stale worker is invisible from the outside — this is a documented,
  expensive bug from the previous project, imported here as a rule rather than a lesson to re-learn.
- **`/health/ready` must probe Celery**, not just Postgres and Redis. A stopped worker otherwise looks
  exactly like a healthy system while every export queues forever.
- Redis becomes load-bearing for a user-visible feature. Redis down is no longer "slower"; it is "no
  exports and no purges". Say so in the runbook rather than discovering it.
- The task function is a thin adapter: it resolves dependencies, calls the application use case, and
  translates the outcome. **No business logic in a Celery task** — a task is an entry point, exactly
  like an HTTP route, and it should be as thin as one.
- Tasks must be **idempotent**. A retried export must not produce two files or two rows; key it on the
  `ExportJob` id.

## Amendment: 2026-10-02, from the plan of slice 2.5 (`identity-email-verification`)

This ADR decided *a* queue and left the topology to the slices that needed more. Three have, each
for its own reason, and the fourth is added now. The topology is recorded here so it lives in one
place.

**(a) The worker consumes four queues, and each exists for a stated reason.**

| Queue | Carries | Why it is separate | Since |
|---|---|---|---|
| `celery` (default) | the beat sweeps: stale tailoring runs, stale export jobs, the guest purge, the identity token sweep | recovery and hygiene must not queue behind the workload they clean up after (ADR-0018 decision 9) | the start |
| `tailoring` | one paid LLM call per run | the expensive workload, separable by `-Q` | 1.3 (ADR-0014) |
| `export` | PDF and DOCX renders | CPU-bound, separable from the paid call | 1.5 (ADR-0016) |
| **`mail`** | account mail deliveries (ADR-0026) | **the one workload a person waits on with nothing to watch** | 2.5 |

`mail` changes nothing about capacity today: the queues share one worker at `--concurrency=2`, so two
tailoring runs can still hold both slots for ~25 s while a confirmation waits. What it buys is the
same thing `tailoring` bought in 1.3: the wait is visible per workload, and a dedicated mail worker is
a `-Q` change rather than a redesign. The trigger for that worker is a confirmation mail measured
waiting behind tailoring.

**(b) The healthy broker holds four members in `_kombu.binding.celery`.** Every queue is bound on the
one default `celery` exchange, so kombu keeps their bindings in a single set named after the
exchange; there is no `_kombu.binding.mail`. The broker is Redis db 1 — read it with `-n 1`, or every
set looks empty for the wrong reason. A fifth member is a stale binding: one publish could then reach
two queues, which for this queue means **two mails** for one request. Adding a queue to
`task_queues` and restarting the dev worker adds a binding, and a local mutation of that declaration
re-poisons the dev broker the same way, because the worker restarts on the edited file.

**(c) The release script's anchored queue check lists all four.** It asserts the worker consumes
`celery`, `tailoring`, `export` and `mail`, each matched on `* {'name': '<queue>'` because the exchange
is also named `celery` and an unanchored match would report the default queue present when it is gone.
`api`, `worker` and `beat` remain the containers that run application code; 2.5 adds no container in
production (Mailpit is dev and CI only).
