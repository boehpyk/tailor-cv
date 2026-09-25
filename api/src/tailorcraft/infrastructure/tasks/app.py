"""The Celery application.

Tasks in this codebase are **thin entry points**. A task resolves its dependencies, calls an
application use case, and translates the outcome — exactly like an HTTP route, and it should be as
short as one. Business logic in a task is logic that can only be exercised by running a worker
(ADR-0005).

The beat schedule holds two jobs, one per aggregate that a lost worker can strand: the stale-run
sweep (slice 1.3, G-25') and the stale-**job** sweep (slice 1.5, X-29, AC-20). They are two entries
rather than one generalized sweep for the reason ADR-0016 (c) gives — the two aggregates have
different terminal states, different failure reasons and different windows, and a shared sweep would
have to be widened by whichever of them grew a third.

**A third entry, the guest-retention purge (FR-6, slice 1.6), exists only when
`GUEST_PURGE_ENABLED` is true, and it ships false** (ADR-0018 decision 5). It is not a sweep and it
does not recover anything: it issues a `DELETE` against rows and unlinks files, and neither is
reversible, so the schedule stays off until the purge has been rehearsed by hand on real data
through `purge-guests` (docs/infrastructure.md's runbook). The flag gates the *entry*, not the task
— the module is always imported and the task is always registered, because a worker that could not
run a message beat publishes is a worse failure than an idle registration. See
`_guest_purge_schedule`.

**The worker's observability is configured here, by Celery signal, and that is not decoration.**
`create_app`'s lifespan calls `configure_logging` / `configure_sentry` for the API process; nothing
called them for the worker, which means the worker — the process that actually holds a CV in memory
for twelve seconds — ran with stdlib logging defaults and, more to the point, **without
`_SILENCED_VENDOR_LOGGERS`**. That list exists because 1.2's `/verify` caught `httpx` logging full
URLs at INFO one frame below a clean adapter; the Gemini SDK is built on `httpx`, so the leak that
was closed in the API was wide open in the worker. See `_on_worker_start` below.
"""

from __future__ import annotations

from typing import Any, Final

from celery import Celery
from celery.signals import celeryd_init, worker_process_init
from kombu import Queue

from tailorcraft.infrastructure.observability import configure_logging, configure_sentry
from tailorcraft.infrastructure.settings import Settings, get_settings

# `as` is an explicit re-export: `tasks.app.TASK_TIME_LIMIT_SECONDS` was this module's name for the
# limit before it moved to `limits.py`, and existing importers keep it.
from tailorcraft.infrastructure.tasks.limits import (
    TASK_TIME_LIMIT_SECONDS as TASK_TIME_LIMIT_SECONDS,
)
from tailorcraft.infrastructure.tasks.limits import refuse_stale_windows_within_time_limit

# The queue Celery publishes to when nothing says otherwise — Celery's own default name, written
# down rather than left implicit, because `task_queues` below turns the set of consumed queues into
# something explicit and a default that is not in the list is a task that vanishes.
DEFAULT_QUEUE_NAME = "celery"

# The stale-run sweep's task name, written once. It lives here rather than beside the task because
# `beat_schedule` below names it and `tasks/tailoring_sweep.py` imports this module: the other
# direction is an import cycle. A schedule and a task that disagree about a name produce
# `NotRegistered` on the worker, once a minute, in the log nobody reads.
ABANDON_STALE_TAILORING_RUNS_TASK_NAME: Final = "tailorcraft.tailoring.abandon_stale_runs"

# How often beat publishes the sweep, and how long each tick stays worth running. The expiry is
# **below** the interval, so at most one live tick exists at any moment: see `beat_schedule`.
STALE_RUN_SWEEP_INTERVAL_SECONDS: Final = 60.0
STALE_RUN_SWEEP_EXPIRES_SECONDS: Final = 55.0

# The stale-**job** sweep's task name (slice 1.5, X-29, AC-20), here for the same reason the run
# sweep's is: `beat_schedule` names it and `tasks/export_sweep.py` imports this module, so the other
# direction is an import cycle.
ABANDON_STALE_EXPORT_JOBS_TASK_NAME: Final = "tailorcraft.export.abandon_stale_jobs"

# Its interval and expiry. **The same two numbers as the run sweep's, and deliberately two more
# constants rather than a reuse of those.** They are equal today by agreement, not by dependency:
# these govern a different schedule over a different aggregate, and the day a long render backlog
# makes the job sweep worth running every 30 s, that change must not silently halve the run sweep's
# interval too. Shared shape is not shared meaning (CLAUDE.md). The expiry is below the interval for
# the reason written at `beat_schedule` below.
STALE_EXPORT_SWEEP_INTERVAL_SECONDS: Final = 60.0
STALE_EXPORT_SWEEP_EXPIRES_SECONDS: Final = 55.0

# The guest purge's task name (slice 1.6, FR-6, ADR-0018), here for the reason the two sweep names
# are: `beat_schedule` below names it and `tasks/retention.py` imports this module, so the other
# direction is an import cycle. A schedule and a task that disagree about a name produce
# `NotRegistered` on the worker, hourly, in a log nobody reads — and for *this* job that failure is
# invisible in every other way, because a purge that never ran looks exactly like a purge that found
# nothing to do (R-10).
PURGE_EXPIRED_GUEST_SESSIONS_TASK_NAME: Final = "tailorcraft.retention.purge_expired_guest_sessions"

# Hourly, with the expiry below the interval for the same reason the sweeps' is: at most one live
# tick exists, so a worker back after a day finds one purge worth running rather than twenty-four.
# An expired tick loses nothing — the next one purges everything the last would have, because the
# work is defined by a predicate over the data rather than by the message.
#
# **Hourly rather than at the retention boundary**, and the difference is the point: `expires_at` is
# per session, so there is no single boundary to fire at. A guest who arrives at 14:37 expires at
# 14:37 the next day, and the promise is "at most 24 hours", so the job must run often enough that
# the overshoot is an hour rather than a day. `docs/infrastructure.md`'s runbook is written against
# this number.
GUEST_PURGE_INTERVAL_SECONDS: Final = 3600.0
GUEST_PURGE_EXPIRES_SECONDS: Final = 3500.0

# How old the heartbeat may get before `/health/ready` calls the purge `stale` — **three missed
# ticks**, derived from the interval above rather than written as a number, so halving the interval
# cannot silently leave the staleness bound three times too generous. One missed tick is a busy
# worker; three is a fault worth a human.
#
# It is a *report*, never a 503 (ADR-0019, AC-32): readiness is about serving a request, and a
# background hygiene job that is behind does not make this process unable to answer one. Pulling the
# app out of service over it would fail a deploy's readiness gate for a job the deploy just
# restarted.
GUEST_PURGE_STALE_AFTER_SECONDS: Final = int(3 * GUEST_PURGE_INTERVAL_SECONDS)  # 10800 (3 h)

# `TASK_TIME_LIMIT_SECONDS` — the hard time limit the config below, the stale-window refusals and
# `PURGE_LOCK_TTL_SECONDS` immediately underneath all read — lives in `tasks/limits.py` with the
# refusals it bounds, so `cli check-settings` can run them without building this app (T46, OQ-2).
# It is imported above and stays importable from here.

# How long the guest purge's Redis lock lives before Redis expires it on its own (slice 1.6,
# ADR-0018 decision 7). It must outlast the longest run that can possibly still be holding it, and
# that bound is the hard time limit above: a pool child is killed at 180 s, so a purge task cannot
# still be working at 240 s. The minute of headroom covers the gap between the kill and the `finally`
# that would have released the lock — a SIGKILLed holder releases nothing, and R-9 then costs exactly
# one skipped hourly tick.
#
# **Deliberately not a setting.** A setting would need a startup guard refusing a TTL at or below the
# time limit, and that would be the *fourth* refusal in this codebase that cannot exit the container
# under `uvicorn --workers N` — a known, measured, still-open bug carried since slice 1.3 (AC-30).
# A constant derived from the very limit it must exceed cannot be misconfigured. The best guard is
# the one you do not need.
#
# It lives here, beside the limit it derives from, rather than in `infrastructure/retention/lock.py`:
# a derivation split from its input is a derivation that stops being one. The lock module does **not**
# import it — it takes the TTL as a constructor argument, so that taking a lock does not oblige a
# process to build a Celery app (see that module's `__init__`); the composition roots pass this.
PURGE_LOCK_TTL_SECONDS: Final = TASK_TIME_LIMIT_SECONDS + 60


def create_celery() -> Celery:
    """Build the Celery application from settings.

    Raises:
        MisconfiguredSettings: `tailoring_stale_after_seconds` is not above the hard time limit.
        MisconfiguredSettings: `export_stale_after_seconds` is not above the hard time limit.
    """
    settings = get_settings()

    # The two stale-window refusals (tailoring and export), in every environment. They live in
    # `tasks/limits.py` — see there for why each window must exceed the hard time limit — so that
    # `cli check-settings` runs the very same checks without building a Celery app (T46, OQ-2).
    #
    # **This runs at API import as well**, not only in the worker and beat:
    # `infrastructure/api/main.py` imports `app` from this module to publish tasks. Under
    # `uvicorn --workers N` a refusal here respawns the failing import for ever rather than exiting,
    # which is why the production `api` command runs `check-settings` first.
    refuse_stale_windows_within_time_limit(settings)

    celery_app = Celery(
        "tailorcraft",
        broker=settings.celery_broker_url,
        backend=settings.celery_result_backend,
        # Task modules to import at worker start. A task the worker never imported is not
        # registered, and a message for it is rejected as `NotRegistered` — a run that fails for a
        # reason that has nothing to do with tailoring. Strings, not imports: this module is
        # imported *by* `tasks/tailoring.py`, and Celery resolves these lazily at finalization.
        include=[
            "tailorcraft.infrastructure.tasks.tailoring",
            "tailorcraft.infrastructure.tasks.tailoring_sweep",
            "tailorcraft.infrastructure.tasks.export",
            "tailorcraft.infrastructure.tasks.export_sweep",
            # The guest purge (slice 1.6). Listed **unconditionally**, although the beat entry below
            # is conditional: `GUEST_PURGE_ENABLED` decides whether anything *publishes* the task,
            # never whether a worker can run one. A worker that skipped the import would reject a
            # message published by a beat that has the flag on — two processes, one `.env`, and the
            # failure would be `NotRegistered` on the one job whose absence is otherwise silent.
            "tailorcraft.infrastructure.tasks.retention",
        ],
    )
    celery_app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # Acknowledge AFTER the task finishes rather than on receipt. **That buys less than it sounds
        # like**, and slice 1.3's /verify found runs stuck `running` for ever in the gap. A message
        # comes back only when the worker's *main* process dies holding it, and then only after
        # `visibility_timeout` below. Everything else is acked:
        #   * a task that raises, or hits `task_time_limit`: `task_acks_on_failure_or_timeout`
        #     defaults to True;
        #   * a pool child that is OOM-killed or SIGKILLed: `task_reject_on_worker_lost` is unset.
        #
        # **`task_reject_on_worker_lost` stays unset, deliberately.** Setting it would redeliver a
        # killed child's message at once, straight into `ExecuteTailoringRun` step 3. Step 3 would
        # find a `running` run inside the stale window and return SKIPPED, so the redelivery recovers
        # nothing. For a message that kills its child every time, it also sets up a redelivery loop,
        # a caveat Celery's own docs give. The stale-run sweep (`tasks/tailoring_sweep.py`) is the one
        # recovery mechanism, for the reason there is one retry mechanism (ADR-0014 §6): two paths
        # that overlap are harder to reason about than one that covers every case.
        #
        # Late acks still require idempotent tasks, and every task here is one.
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        # An export the user is waiting on must fail visibly rather than hang forever.
        task_soft_time_limit=120,
        task_time_limit=TASK_TIME_LIMIT_SECONDS,
        result_expires=3600,
        # --- The Redis redelivery window (slice 1.3) --------------------------------------------
        #
        # Redis has no broker-side acks. kombu parks each delivered-but-unacked message in a hash
        # and puts it back on its queue once it is older than `visibility_timeout`, **whether or
        # not the worker holding it is still alive**. So this one number means two things: how long
        # a message stays lost after a worker's main process dies holding it, and how long a
        # healthy task may stay unacked before a second copy is delivered.
        #
        # 600 s, against kombu's default of 3600:
        #   * Well above `task_time_limit` (180), so a healthy task is never duplicated. With
        #     `worker_prefetch_multiplier=1` a message stays unacked for little more than its own
        #     run, and the hard limit caps that run.
        #   * Above `tailoring_stale_after_seconds` (300). A restored message for a lost `running`
        #     run therefore arrives after the sweep has recorded that run, and step 3 finds it
        #     decided.
        #   * A `queued` run whose message was lost with a SIGKILLed main process is delivered again
        #     after ten minutes rather than an hour. **That is the only recovery a `queued` run
        #     has**: the sweep looks only at `running` runs.
        #
        # **This also bounds any future `countdown`/`eta` task.** On Redis, a message scheduled
        # further ahead than this is restored and delivered again every 600 s until it runs, which
        # is Celery's documented caveat. Revisit this number before adding one.
        #
        # Set in the shared config so the API, which publishes, and the worker, which restores,
        # agree on one value.
        broker_transport_options={"visibility_timeout": 600},
        # --- The named queues (slice 1.3) ------------------------------------------------------
        #
        # Tailoring gets its **own** named queue from the first slice that needs one. One line now,
        # expensive to retrofit: 1.5's PDF and DOCX renders share this worker, and a long render
        # holding both of `--concurrency=2`'s slots would starve a tailoring run the user is
        # watching a spinner for. Separate queues are what let the two workloads be given separate
        # capacity later without touching either task.
        #
        # **The failure mode this list exists to prevent: a task published to `tailoring` while the
        # worker consumes only `celery` is a run that queues forever — and it looks exactly like a
        # healthy system.** The API answers, the row is committed `queued`, Redis is up, the worker
        # is up and reports itself idle, `/health/ready` is green, and nothing anywhere logs an
        # error. The only symptom is a spinner that never stops. That is why the queue list is
        # declared here, derived from the same `Settings` field the producer publishes with
        # (`CeleryTailoringQueue` reads `settings.tailoring_queue_name` too), rather than hard-coded
        # into the worker's command line in `docker-compose.yml` where it could drift out of step
        # with the producer in a one-word edit — and why the deploy verifies the running worker's
        # queue list rather than trusting either file (docs/cicd.md, and T36).
        #
        # A worker started with no `-Q` consumes exactly `task_queues`, so both entries below are
        # live. `-Q` on the command line still overrides this, which is what makes "give tailoring
        # its own worker" a compose change and not a code change.
        #
        # **Each queue names its own routing key, and the key is load-bearing.** A `Queue` declared
        # without one gets `task_default_routing_key`, which is `celery`. Both queues were therefore
        # bound to the `celery` exchange under the *same* key: kombu's lookup for
        # `exchange='celery', routing_key='celery'` returned `['celery', 'tailoring']`, measured at
        # /verify round 1. A message published that way lands in both queues. For a tailoring run
        # that means two deliveries of one run, and `ExecuteTailoringRun`'s idempotency is
        # read-then-write, so two workers can both read `queued` and both pay for the call. Nothing
        # would log it as an error.
        #
        # Today's publishers were unaffected. `send_task(queue=...)`, used by
        # `CeleryTailoringQueue` and by beat's `options={"queue": ...}`, publishes to the anonymous
        # exchange with the queue's name as the key, and that reaches exactly one queue. The shared
        # key was a trap set for the first `task_routes` entry or explicit `exchange=` publish.
        #
        # **Redis remembers bindings.** kombu adds one when a queue is declared and never removes
        # one. Any broker that ran the old declaration keeps the stale binding until someone deletes
        # it: `SREM _kombu.binding.celery "celery\x06\x16\x06\x16tailoring"`.
        #
        # **The third queue, `export` (slice 1.5, AC-44), for the reason the second one exists.** A
        # PDF render holds a worker slot for a second or two; `--concurrency=2` means two of them can
        # hold both slots while a tailoring run the user is watching a spinner for waits. One worker
        # consumes all three today, so this separates the messages and not yet the capacity — and
        # that is exactly the point: giving exports their own worker is then `-Q export` in
        # `docker-compose.yml` rather than a code change.
        #
        # It was written **with its routing key in its first version**, and that sentence is the
        # whole of the lesson above applied forward rather than recorded. kombu adds a binding when
        # a queue is declared and never removes one; `watchmedo` restarts the worker the moment this
        # file is saved; so a version of this line without `routing_key=` would have reached the dev
        # broker before anyone chose to run it, bound `export` under the key `celery`, and left a
        # publish on `celery` reaching two queues until someone ran `SREM` by hand. There is no
        # version of this declaration without the key anywhere in this branch's history.
        task_default_queue=DEFAULT_QUEUE_NAME,
        task_queues=(
            Queue(DEFAULT_QUEUE_NAME, routing_key=DEFAULT_QUEUE_NAME),
            Queue(settings.tailoring_queue_name, routing_key=settings.tailoring_queue_name),
            Queue(settings.export_queue_name, routing_key=settings.export_queue_name),
        ),
        # --- The beat schedule --------------------------------------------------------------------
        #
        # Two sweeps, plus the guest purge when it is enabled. See the module docstring for why the
        # sweeps are two entries and not one, and `_guest_purge_schedule` for why the third is
        # conditional and the other two are not.
        beat_schedule={
            "abandon-stale-tailoring-runs": {
                "task": ABANDON_STALE_TAILORING_RUNS_TASK_NAME,
                "schedule": STALE_RUN_SWEEP_INTERVAL_SECONDS,
                "options": {
                    # **The default `celery` queue, not `tailoring`.** The sweep recovers runs the
                    # tailoring workload lost, and queueing it behind a backlog of paid runs would
                    # make recovery wait on the very workload that failed.
                    #
                    # Today one worker consumes both queues, so this separates the messages, not the
                    # capacity: a sweep still needs a free slot. It becomes separate capacity the
                    # day tailoring gets its own worker (`-Q`, a compose change), and this line is
                    # already right for that day.
                    "queue": DEFAULT_QUEUE_NAME,
                    # **Expires before the next tick is published.** Beat keeps publishing while the
                    # worker is down. Without an expiry, a worker back after an hour would find sixty
                    # identical sweeps, each one after the first finding nothing, all of them queued
                    # ahead of real work. A tick is worth nothing once a newer one exists, so the
                    # worker discards an expired one unrun.
                    #
                    # Under sustained load that keeps every slot busy past 55 s, ticks can expire
                    # unrun as well. That delays recovery and never loses it: a stale run stays
                    # listed until a tick finds a free slot.
                    "expires": STALE_RUN_SWEEP_EXPIRES_SECONDS,
                },
            },
            # The stale-**job** sweep (slice 1.5, X-29, AC-20): the one recovery for an export whose
            # worker was lost. Redelivery is not, for the reason written at `task_acks_late` above —
            # a killed pool child, a hard time limit and a task that raised all ack their message —
            # so without this entry every one of those leaves a job `rendering` for ever behind a UI
            # that says "still preparing your file".
            "abandon-stale-export-jobs": {
                "task": ABANDON_STALE_EXPORT_JOBS_TASK_NAME,
                "schedule": STALE_EXPORT_SWEEP_INTERVAL_SECONDS,
                "options": {
                    # **The default `celery` queue, not `export`** — the run sweep's reason exactly:
                    # queueing the recovery behind the backlog of renders it is recovering from
                    # makes recovery wait on the workload that failed. It becomes separate capacity
                    # the day exports get their own worker (`-Q`, a compose change), and this line
                    # is already right for that day.
                    "queue": DEFAULT_QUEUE_NAME,
                    # Below the interval, so at most one live tick exists: a worker back after an
                    # hour finds one sweep worth running rather than sixty, and the fifty-nine it
                    # discards were each made pointless by the one after it. A tick that expires
                    # unrun delays recovery and never loses it — a stale job stays listed until a
                    # tick finds a free slot.
                    "expires": STALE_EXPORT_SWEEP_EXPIRES_SECONDS,
                },
            },
            # The guest purge (slice 1.6, FR-6), **present only when `GUEST_PURGE_ENABLED` is
            # true** — see `_guest_purge_schedule`. A `**` merge of either one entry or none,
            # rather than an `if` that mutates the schedule after the fact: the two sweeps above
            # stay byte-for-byte what they were, and the third's condition is one call a reader can
            # follow instead of a second place the schedule is assembled.
            **_guest_purge_schedule(settings),
        },
    )
    return celery_app


def _guest_purge_schedule(settings: Settings) -> dict[str, dict[str, object]]:
    """The hourly guest-purge beat entry, or **nothing at all** (AC-25, ADR-0018 decision 5).

    **The entry is absent rather than disabled when the flag is off**, and that is the difference
    worth stating. Celery has no "paused" entry; a present entry with a huge interval, or one whose
    task returns early on a flag, still publishes messages and still writes a heartbeat, so an
    operator reading `beat`'s startup banner or `/health/ready` would see a job that is scheduled.
    Nothing is what `scheduled: false` means, and an absent key is the only honest spelling of it.

    The flag is read **once, here, at `create_celery`** — so flipping it takes a `beat` restart,
    which is exactly the deploy-shaped, deliberate act this job's first real run should be. A
    setting re-read per tick would let a stray `.env` edit start deleting a stranger's CV without
    anyone running anything.

    **The CLI does not consult it.** `purge-guests` purges whether or not the schedule is on: an
    operator who typed the command has already said what they want, and the rehearsal this flag is
    waiting for is performed with that command. The flag governs the *unattended* run only.
    """
    if not settings.guest_purge_enabled:
        return {}
    return {
        "purge-expired-guest-sessions": {
            "task": PURGE_EXPIRED_GUEST_SESSIONS_TASK_NAME,
            "schedule": GUEST_PURGE_INTERVAL_SECONDS,
            "options": {
                # **The default `celery` queue, and `task_queues` above is untouched** (AC-27,
                # ADR-0018 decision 9). Hygiene must not queue behind the workload it cleans up
                # after — the sweeps' reason, and sharper here: a backlog of paid tailoring runs or
                # slow renders would delay the job that keeps a privacy promise, and the promise has
                # a deadline the queue knows nothing about.
                #
                # A fourth queue would also mean a fourth kombu binding, and kombu adds one per
                # declaration and never removes one. The healthy broker state stays **three members
                # in `_kombu.binding.celery`** on db 1 (all queues share the one default exchange),
                # so there is nothing to `SREM` after this slice.
                "queue": DEFAULT_QUEUE_NAME,
                # Below the interval, so at most one live tick exists — the shape both sweeps use.
                # A tick that expires unrun costs nothing here: the next one deletes everything this
                # one would have, because the work is a predicate over the data and not a payload in
                # the message. What it does cost is an hour of a promise, which is why the interval
                # is an hour and not a day.
                "expires": GUEST_PURGE_EXPIRES_SECONDS,
            },
        }
    }


# Same narrow ignore as `tasks/tailoring.py`'s: `celery.signals` is untyped, so `.connect` would
# make this handler untyped. Its body stays strictly checked.
@celeryd_init.connect  # type: ignore[untyped-decorator]  # celery is untyped
def _on_worker_start(**_kwargs: Any) -> None:
    """Give the worker process the same logging and error-reporting configuration the API has.

    `celeryd_init` fires once in the main worker process, before the pool forks;
    `worker_process_init` fires in each forked child. Both are connected, because `structlog`'s
    configuration and the silenced vendor loggers are per-*process* state: with `--concurrency=2`
    and the default prefork pool, the children are where tasks actually run, and a child that
    inherited an unconfigured `logging` root is a child whose `httpx` records are not silenced.
    Configuring twice is idempotent and cheap; configuring once in the wrong process is a silent
    privacy regression.

    `configure_sentry` is a no-op without a DSN, so this costs nothing in dev and in CI.
    """
    settings = get_settings()
    configure_logging(settings)
    configure_sentry(settings)


# The same handler on the child-process signal — see `_on_worker_start`'s docstring for why both.
worker_process_init.connect(_on_worker_start)


app = create_celery()
