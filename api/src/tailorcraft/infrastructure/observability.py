"""Structured logging and error reporting.

Two rules shape this file, and both come from documented pain:

1. **Sentry is configured in Phase 0**, not deferred. The previous project deferred it as "cheap
   insurance, later" and then spent a session debugging a crash-looping worker while every health
   check reported green. This product has silent failure paths from its first slice.
2. **Logs are structured and they never contain a CV.** Constitution §8: log ids, sizes, durations,
   token counts, outcomes. Never CV text, never a prompt body, never a completion. A debug log is
   the easiest way to leak a stranger's home address into a file nobody thinks of as a database.

JSON to stdout, collected by Docker. Human-readable output in dev, because a developer reading a
terminal is a different consumer from a log aggregator and pretending otherwise helps neither.
"""

from __future__ import annotations

import logging
import sys

import sentry_sdk
import structlog

from tailorcraft.infrastructure.settings import Settings


def configure_logging(settings: Settings) -> None:
    """Install structlog as the single logging path for the process."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    # A console renderer in dev, JSON everywhere else.
    processors.append(
        structlog.dev.ConsoleRenderer()
        if settings.app_env == "dev"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def configure_sentry(settings: Settings) -> None:
    """Initialise error reporting, if a DSN is configured.

    No DSN in dev or CI, and that is fine: the call is a no-op rather than a branch every caller has
    to remember. `send_default_pii=False` is not a default we are accepting — it is a decision. This
    application handles CVs, and the difference between an error report and a data leak is exactly
    which request body Sentry attaches to it.
    """
    if not settings.sentry_dsn:
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        send_default_pii=False,
        traces_sample_rate=0.1,
        # Bodies can contain a CV. Never ship them.
        max_request_body_size="never",
    )
