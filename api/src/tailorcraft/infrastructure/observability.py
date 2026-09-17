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
#
# `httpx` was added by slice 1.2, and it is the sharpest entry in this tuple. `httpx` logs every
# request it makes at INFO, in full:
#
#     INFO httpx:_client.py:1740  HTTP Request: GET https://jobs.example.com/postings/1234?ref=abc "200 OK"
#
# That is a job-posting URL — the specific job a specific, probably anxious person is applying for —
# written to the application log on every fetch, complete with path and query (Constitution §8 names
# those as never-loggable, and job boards routinely put tracking and referral tokens in the query).
# `HttpxTrafilaturaFetcher` is scrupulous about logging only `url.host`; none of that mattered while
# the library it calls was logging the whole URL one frame away.
#
# Found by the AC-18 test that runs against the REAL adapter. The pre-existing privacy test drove a
# FAKE fetcher through `app.dependency_overrides`, so no `httpx` request was ever made and the leak
# could not appear — a test whose name promised coverage its assertions could not deliver.
# `httpcore` is httpx's transport and a SEPARATE top-level logger namespace — not a child of
# `httpx`, so silencing that name does not reach it. It was measured before being added here and it
# does not currently leak: at DEBUG with every logger open it emitted six records carrying `host`
# and `port` only, and it elides request/response reprs (`<Request [b'GET']>`). It is silenced
# anyway, because the sentence above says the library never gets to speak and that should be true
# rather than true-today: httpcore's messages are debug-gated, and "nothing leaks as long as nobody
# sets LOG_LEVEL=debug while chasing a stuck fetch" is not a guarantee, it is a hope.
#
# `google_genai` and `google.genai` were added at slice 1.3's `/verify`, and they are two entries
# because they are two unrelated names to `logging.getLogger` — the underscore is not a typo, and
# neither covers the other. Measured by grepping the installed google-genai (2.23.0) for
# `getLogger(`, not assumed: every SDK module logs under the `google_genai` parent (`models`,
# `_api_client`, `_transformers`, `_common`, `types`, `chats`, `caches`, `batches`, `files`,
# `tokens`, `tunings`, `operations`, `documents`, `live`, `live_music`, `local_tokenizer`,
# `filesearchstores`). One generated module, `_gaos/utils/logger.py`, returns the DOTTED
# `google.genai` logger instead, only when `GOOGLE_GENAI_DEBUG` is set — at which point it calls
# `logging.basicConfig(level=DEBUG)` itself — and the same module defines `get_body_content`, which
# renders a request's body; here that body is the prompt, which is the CV. Silencing the `google`
# parent would have covered both, and every other `google.*` library with them, which is a
# different decision from this one.
#
# T34 measured the non-streaming `generate_content` path this adapter uses and found it clean — two
# static notices from `google_genai.models`, nothing interpolated — and that measurement was right.
# It is not what this entry rests on. `google_genai._api_client` has a DEBUG line on both STREAMING
# paths that interpolates `chunk_dump`, the raw JSON of a response chunk — the completion — into its
# message. The adapter does not stream today; that is the `httpcore` argument above exactly: the
# library never gets to speak, true rather than true-today. The spec's privacy item 4 won over T34's
# "no change needed".
#
# **`weasyprint` and `fontTools` were added by slice 1.5, and they are here for two DIFFERENT
# reasons. Do not collapse them into one.** A reader who assumes both are noise control will one day
# re-enable the wrong one.
#
# `weasyprint` is a **privacy requirement** — 1.2's `httpx` lesson, second instance. It logs a failed
# resource load at **ERROR**, with the URL interpolated into the message. Measured at I15 against a
# hostile fixture carrying a remote stylesheet and a remote image:
#
#     ERROR weasyprint  Failed to load stylesheet at http://jane-doe-private.example/x.css: UrlFetchRefused: This renderer fetches nothing.
#     ERROR weasyprint  Failed to load image at 'http://jane-doe-private.example/portrait-jane-doe.png': UrlFetchRefused: This renderer fetches nothing.
#
# That URL came out of a stranger's CV, and a URL a user typed can carry their name — which is
# exactly X-54's PII case, and exactly why `refuse_every_url` logs `export.url_fetch_refused` with
# the **scheme only** and raises a constant message that names no URL. None of that adapter-side care
# survives the library logging the whole URL one frame away: a vendor's log line is a leak a clean
# adapter cannot prevent, so the library does not get to speak. Its exceptions still reach us, and
# `PDF_DOCUMENT_ERRORS` translates them (`export/pdf.py`). `weasyprint.progress` is a child logger
# and is covered by this one entry through `getEffectiveLevel()`.
#
# `fontTools` is **noise control**, and the number is what justifies it. WeasyPrint subsets every
# embedded font through `fontTools`, which narrates the work: measured at I15, **327 records for one
# render** of the model-CV corpus fixture (111 INFO + 216 DEBUG, across `fontTools.subset`,
# `fontTools.subset.timer` and `fontTools.ttLib.ttFont`), and more on a document using more faces —
#
#     INFO  fontTools.subset        maxp pruned
#     DEBUG fontTools.ttLib.ttFont  Reading 'cmap' table from disk
#     DEBUG fontTools.subset.timer  Took 0.000s to prune 'maxp'
#
# — none of it carrying document content, all of it about the font files WE ship. It is silenced
# because an export's four useful log lines should not be buried under three hundred, not because
# anything in it is dangerous. If a font bug ever needs debugging, dropping this ONE name from the
# tuple is the right move; dropping `weasyprint` is not.
_SILENCED_VENDOR_LOGGERS = (
    "pypdf",
    "docx",
    "httpx",
    "httpcore",
    "google_genai",
    "google.genai",
    "weasyprint",
    "fontTools",
)


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
        # **The third setting, and the one that actually decides it.** `sentry_sdk` defaults
        # `include_local_variables=True`, and neither `send_default_pii=False` nor
        # `max_request_body_size="never"` touches it: they govern the *request*, this governs the
        # **traceback frames**. Any exception escaping a function that holds a CV in a local ships
        # that CV to Sentry, and slice 1.3 creates the worst instance of it in the codebase — the
        # Gemini adapter's local variable is the assembled prompt, which is the entire CV. The
        # adapter defends itself with `raise … from None` so the frame is unreachable; this is the
        # floor underneath that, for every frame nobody thought about. Two settings that sound like
        # they cover PII, one that does (CLAUDE.md's footgun list; feature-spec AC-23).
        include_local_variables=False,
    )
