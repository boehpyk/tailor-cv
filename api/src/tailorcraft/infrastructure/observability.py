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

# Third-party loggers that emit *document content*, silenced at the source. Not noise reduction —
# a privacy control (Constitution §8, AC-12: "nothing in this slice logs any fragment of extracted
# text").
#
# `pypdf` logs through the stdlib `logging` module from inside the extraction worker thread whenever
# a file is damaged-but-parseable, and at least one of those call sites formats content lifted
# straight out of the document: `pypdf/_cmap.py` logs an unparsable character-map line with `%r` of
# the raw line. Because `configure_logging` points `basicConfig` at stdout, those records land in the
# application's own log stream, having passed through none of our review. Every adapter in this
# codebase is careful about what it logs; a vendor library is under no such obligation, and the way
# to make AC-12 structurally true rather than merely untested is to make sure the library never gets
# to speak. Its exceptions still reach us — those we translate ourselves (`intake/extraction.py`).
#
# A level ABOVE `CRITICAL` rather than a filter or `propagate = False`: `Logger.isEnabledFor` is
# consulted before a record is even constructed, so nothing is formatted, and child loggers
# (`pypdf._cmap`, `pypdf.generic...`) inherit it through `getEffectiveLevel()` walking to this
# parent, so one entry covers the package. `propagate = False` alone would not do it — a logger with
# no handlers of its own falls back to `logging.lastResort`, which writes to stderr.
_SILENCED_VENDOR_LOGGERS = ("pypdf", "docx")


def configure_logging(settings: Settings) -> None:
    """Install structlog as the single logging path for the process."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    for name in _SILENCED_VENDOR_LOGGERS:
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)

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
