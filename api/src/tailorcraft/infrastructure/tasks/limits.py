"""The worker's time limits, and the settings refusals they bound.

**Why this is its own module** (slice 2.1, T46, OQ-2): `tasks/app.py` builds the Celery application
at import, so the only way to run these refusals used to be to build one. `python -m tailorcraft.cli
check-settings` runs before uvicorn in the production `api` command, and must be able to ask "would
this configuration start?" without constructing anything — so the checks live here, with no Celery
import, and `create_celery` calls the same function. One definition, two callers: a check-settings
that re-stated the rule would drift from the rule it claims to pre-flight.
"""

from __future__ import annotations

from typing import Final

from tailorcraft.infrastructure.settings import MisconfiguredSettings, Settings

# The hard time limit: the pool child running a task is killed at this many seconds. Named because
# three things read it — `create_celery`'s config, the stale-window refusals below, and
# `tasks/app.py`'s `PURGE_LOCK_TTL_SECONDS` — and a limit written twice is a limit that drifts from
# the check guarding it.
TASK_TIME_LIMIT_SECONDS: Final = 180
# The soft time limit: `SoftTimeLimitExceeded` is raised inside the task at this many seconds. Named
# for the same reason as the hard limit: `create_celery`'s config and the mail-deadline refusal
# below both read it (slice 2.5).
TASK_SOFT_TIME_LIMIT_SECONDS: Final = 120


def refuse_stale_windows_within_time_limit(settings: Settings) -> None:
    """Raise `MisconfiguredSettings` if either stale window is not above the hard time limit.

    Pure: reads two integers off `settings`, builds nothing, touches nothing. Called by
    `create_celery` (every process that imports the Celery app — the API, the worker and beat) and by
    `cli check-settings`.

    The messages carry the offending **integer** setting and the limit. Neither is a secret, and
    naming the number is what makes the message actionable; nothing else from `settings` is read.
    """
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
    # It is not production-only: the sweep acts on whatever `Settings` the worker holds, whatever
    # `APP_ENV` says.
    if settings.tailoring_stale_after_seconds <= TASK_TIME_LIMIT_SECONDS:
        raise MisconfiguredSettings(
            "TAILORING_STALE_AFTER_SECONDS must be greater than Celery's task_time_limit: "
            f"tailoring_stale_after_seconds={settings.tailoring_stale_after_seconds} is not above "
            f"task_time_limit={TASK_TIME_LIMIT_SECONDS}. The stale-run sweep would record a call "
            "that is still running as abandoned, and its paid-for result would be lost. Set it "
            f"above {TASK_TIME_LIMIT_SECONDS} (the default is 300)."
        )

    # **The second guard, beside the first, same shape, same reason.** Slice 1.5's stale-job sweep
    # (`AbandonStaleExportJobs`) is `AbandonStaleTailoringRuns`'s sibling: a live render must never
    # be swept. With `export_stale_after_seconds` at or below the hard limit, the sweep can record a
    # job `abandoned` while its worker is still writing the file, the worker's later `mark_ready`
    # then meets a row the sweep already decided, and the render is lost after being paid for in
    # worker seconds and disk. Above the limit, the pool child is killed before its job is old
    # enough to sweep.
    if settings.export_stale_after_seconds <= TASK_TIME_LIMIT_SECONDS:
        raise MisconfiguredSettings(
            "EXPORT_STALE_AFTER_SECONDS must be greater than Celery's task_time_limit: "
            f"export_stale_after_seconds={settings.export_stale_after_seconds} is not above "
            f"task_time_limit={TASK_TIME_LIMIT_SECONDS}. The stale-job sweep would record a render "
            "that is still running as abandoned, and its paid-for result would be lost. Set it "
            f"above {TASK_TIME_LIMIT_SECONDS} (the default is 300)."
        )


def refuse_mail_deadline_within_soft_limit(settings: Settings) -> None:
    """Raise `MisconfiguredSettings` unless `MAIL_TOTAL_DEADLINE_SECONDS` is below the soft limit.

    Slice 2.5 (technical plan §3, AC-24). Same shape, same two callers, same "every environment" as
    the stale-window refusals above. The SMTP adapter stops *starting* attempts once its deadline
    has passed; an attempt already in flight is bounded by `MAIL_SEND_TIMEOUT_SECONDS` per socket
    operation. If the deadline reached the soft limit, `SoftTimeLimitExceeded` would land inside a
    send instead of the adapter deciding `unavailable` itself, and the task would record no outcome
    at all — a person waiting on a confirmation link, and nothing anywhere saying why it never came.
    Equality is refused: a deadline at second 120 and a soft limit at second 120 is that race.

    The field's own bound (5-120) admits 120 on purpose, so that this rule is what refuses it, by
    name, rather than a `ValidationError` that cannot say why.
    """
    if settings.mail_total_deadline_seconds >= TASK_SOFT_TIME_LIMIT_SECONDS:
        raise MisconfiguredSettings(
            "MAIL_TOTAL_DEADLINE_SECONDS must be below Celery's task_soft_time_limit: "
            f"mail_total_deadline_seconds={settings.mail_total_deadline_seconds} is not below "
            f"task_soft_time_limit={TASK_SOFT_TIME_LIMIT_SECONDS}. A send could then be interrupted "
            "by the soft limit before the mail adapter records an outcome. Set it below "
            f"{TASK_SOFT_TIME_LIMIT_SECONDS} (the default is 30)."
        )
