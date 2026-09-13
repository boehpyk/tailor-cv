"""The Celery application.

Tasks in this codebase are **thin entry points**. A task resolves its dependencies, calls an
application use case, and translates the outcome — exactly like an HTTP route, and it should be as
short as one. Business logic in a task is logic that can only be exercised by running a worker
(ADR-0005).

The beat schedule holds one job: the stale-run sweep (slice 1.3, G-25'), which records a run whose
worker was lost. The guest-retention purge (FR-6) lands with slice 1.6, and the roadmap deliberately
keeps it *off* until it has been rehearsed by hand on real data. It issues a `DELETE` against rows and
unlinks files, and neither is reversible.

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
from tailorcraft.infrastructure.settings import MisconfiguredSettings, get_settings

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

# The hard time limit: the pool child running a task is killed at this many seconds. Named because
# two things read it, the config below and the stale-window check at the top of `create_celery`,
# and a limit written twice is a limit that drifts from the check guarding it.
TASK_TIME_LIMIT_SECONDS: Final = 180


def create_celery() -> Celery:
    """Build the Celery application from settings.

    Raises:
        MisconfiguredSettings: `tailoring_stale_after_seconds` is not above the hard time limit.
    """
    settings = get_settings()

    # **Refuse a stale window that is not above the hard time limit, in every environment.** Only
    # this ordering keeps the sweep from deciding a live call. With a window at or below the limit,
    # this sequence can happen:
    #   1. The sweep records a run `abandoned` while its worker is still waiting on the model.
    #   2. The worker's later `succeeded` save meets the row the sweep already decided, and the
    #      table's CHECK constraints reject it.
    #   3. The documents are lost after being paid for, and the user sees "That run was
    #      interrupted", with **Try again** inviting them to pay a second time.
    # Above the limit, the pool child is killed before its run is old enough to sweep. At the 181 s
    # boundary the margin is about a second (whole-second `started_at`, strict `>`), and it holds
    # only while the worker's timer fires on time. At the 300 s default the margin is wide.
    # Equality is refused too: a kill at second 180 and a sweep judging the same run at second 180
    # is exactly the race this rules out.
    #
    # Checked here because this is the one place the setting and the limit meet. It is not
    # production-only: the sweep acts on whatever `Settings` the worker holds, whatever `APP_ENV`
    # says.
    #
    # **This runs at API import as well**, not only in the worker and beat:
    # `infrastructure/api/main.py` imports `app` from this module to publish tasks. The refusal
    # therefore inherits CLAUDE.md's `uvicorn --workers N` footgun. Under the production image's
    # `--workers 2`, the supervisor respawns the failing import for ever, and the container never
    # becomes ready and never exits. Worker and beat fail loudly by contrast: the celery CLI cannot
    # load the app, so the process exits and `restart: unless-stopped` shows a restart loop. When a
    # release's API never goes ready, read its log for this message. Not solved here.
    if settings.tailoring_stale_after_seconds <= TASK_TIME_LIMIT_SECONDS:
        raise MisconfiguredSettings(
            "TAILORING_STALE_AFTER_SECONDS must be greater than Celery's task_time_limit: "
            f"tailoring_stale_after_seconds={settings.tailoring_stale_after_seconds} is not above "
            f"task_time_limit={TASK_TIME_LIMIT_SECONDS}. The stale-run sweep would record a call "
            "that is still running as abandoned, and its paid-for result would be lost. Set it "
            f"above {TASK_TIME_LIMIT_SECONDS} (the default is 300)."
        )

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
        task_default_queue=DEFAULT_QUEUE_NAME,
        task_queues=(
            Queue(DEFAULT_QUEUE_NAME, routing_key=DEFAULT_QUEUE_NAME),
            Queue(settings.tailoring_queue_name, routing_key=settings.tailoring_queue_name),
        ),
        # --- The beat schedule --------------------------------------------------------------------
        #
        # One job until slice 1.6: the stale-run sweep (G-25'). See the module docstring.
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
        },
    )
    return celery_app


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
