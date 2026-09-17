"""`MarkdownDocumentRenderer` — the `DocumentRendererPort` adapter. ADR-0017, AC-30/31/32/34.

The facade over the pipeline, and deliberately almost nothing else: a `match` over the four formats,
a thread, a timeout, a size check, the translations, and four log lines. Every decision with any
content in it lives in a pure function in its own module (`tokens`, `plain_text`, `html`, `docx`,
`pdf`), which is what lets AC-28's hostile table run in microseconds without this class existing at
all. What is left here is the part that cannot be pure: a clock, a thread pool, a settings object
and the boundary where four vendors' failures become one domain error family.

**It imports `pdf.render_pdf`, never `weasyprint`.** `pdf.py` is the only module in the codebase
that imports the layout engine (ADR-0017's adapter table), and it stays that way — which is also why
the two tuples of measured vendor exception types (`PDF_DOCUMENT_ERRORS`, `DOCX_DOCUMENT_ERRORS`)
are defined beside their own vendor and imported here by name. The same rule with a second reason
applies to `docx`: this module imports the HTML emitter, and AC-31's import-graph test forbids any
module from importing both the vendor `docx` and `html.py`. Importing *our* `export.docx` walker is
the point of a facade; importing the library it wraps would not be.

**Three things this adapter is responsible for that nothing else can be:**

1. **Nothing synchronous runs on the event loop.** Every one of the four renders goes through
   `asyncio.to_thread` under `asyncio.wait_for`, including `md`, which is one `str.encode`. The
   uniformity is the point: the day somebody adds a parse to the Markdown branch there is no
   "cheap" path to forget to move. A CPU-bound call in an async route stalls *every* concurrent
   user and presents as "the app is slow", never as an error (CLAUDE.md; 1.1's DOCX sniff stalled
   the loop 374 ms).
2. **The timeout is chosen by `format.delivery`**, so one adapter serves both processes — 5 s inside
   an API request, 60 s in the worker. That one fact on the enum is what makes the API's inline
   render and the worker's queued render the same code path with different bounds, instead of two
   adapters that drift.
3. **`DocumentRendererPort.render` promises a `DocumentRenderFailed` subclass on every failure**, and
   this module is where that promise is made true **structurally**: an `except Exception` floor
   underneath, the measured vendor types on top carrying the better reason. An allow-list alone is a
   bet that you enumerated every way four libraries can fail on input a stranger chose, and CLAUDE.md
   records losing exactly that bet in the extraction sweep. `Exception`, never `BaseException` —
   `asyncio.CancelledError` must still cancel a worker shutting down (X-36).

**Privacy is the reason the failure path looks the way it does** (AC-32, AC-34, Constitution §8).
When something raises, this frame holds the Markdown, the token list, the HTML string *and* the
rendered bytes — four copies of a stranger's employment history — and `sentry_sdk` defaults
`include_local_variables=True`. So: the log line carries the exception's fully-qualified **type** and
never its message (WeasyPrint quotes the offending CSS declaration, `python-docx` quotes XML), and
every translated error is raised **`from None`**, which cuts the `__cause__` chain that would
otherwise keep this frame reachable from a Sentry report. There is no `exc_info` anywhere in this
module.

**The testing seams are constructor arguments with strict defaults** — `HttpxTrafilaturaFetcher`'s
and `GeminiLlm`'s pattern. Nothing under `api/src/` passes either one, and a wiring test asserts the
production binding takes both defaults. A seam production could take by accident is not a seam.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Final, assert_never

import structlog
from markdown_it.token import Token

from tailorcraft.domain.export.errors import (
    DocumentRenderError,
    DocumentRenderFailedOnDocument,
    DocumentRenderOutputTooLarge,
    DocumentRenderTimedOut,
)
from tailorcraft.domain.export.value_objects import ExportDelivery, ExportFormat
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind
from tailorcraft.infrastructure.export.docx import DOCX_DOCUMENT_ERRORS, render_docx
from tailorcraft.infrastructure.export.html import (
    render_body_fragment,
    sanitize_html,
    wrap_in_document,
)
from tailorcraft.infrastructure.export.pdf import (
    PDF_DOCUMENT_ERRORS,
    UrlFetcher,
    refuse_every_url,
    render_pdf,
)
from tailorcraft.infrastructure.export.plain_text import render_plain_text
from tailorcraft.infrastructure.export.tokens import normalize_to_grammar, parse_document
from tailorcraft.infrastructure.settings import Settings

if TYPE_CHECKING:
    from tailorcraft.domain.export.ports import DocumentRendererPort

log = structlog.get_logger(__name__)

_EVENT_STARTED: Final[str] = "export.render_started"
_EVENT_SUCCEEDED: Final[str] = "export.render_succeeded"
_EVENT_FAILED: Final[str] = "export.render_failed"

# The specific translations, both vendors' measured types in one tuple — caught **above** the floor
# so that a failure the pipeline has a name for is recorded `render_failed` (X-25, the document's own
# fault, no "Export again" offered) rather than as the residual `render_error` (X-37, retryable).
# Each half is defined beside the vendor that raises it; see those two modules for what was measured
# and against which version.
_DOCUMENT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    *PDF_DOCUMENT_ERRORS,
    *DOCX_DOCUMENT_ERRORS,
)


class MarkdownDocumentRenderer:
    """Turn one document's Markdown into one format's bytes, or into a failure the domain names."""

    def __init__(
        self,
        settings: Settings,
        *,
        url_fetcher: UrlFetcher = refuse_every_url,
        sanitize: Callable[[str], str] = sanitize_html,
    ) -> None:
        self._settings = settings
        self._url_fetcher = url_fetcher
        self._sanitize = sanitize

    async def render(
        self, markdown: str, *, document: TailoredDocumentKind, format: ExportFormat
    ) -> bytes:
        """Render one document into one format's bytes.

        Raises:
            DocumentRenderFailed: always a subclass of it, never a bare exception from WeasyPrint,
                `python-docx`, markdown-it or `nh3` — see the module docstring for how that is
                guaranteed structurally rather than by enumeration.
        """
        timeout_seconds = self._timeout_for(format)
        started_at = time.perf_counter()
        # `character_count`, never the characters. The size of a CV is an operational fact; its text
        # is the thing this whole module is built around not writing down (AC-32).
        log.info(
            _EVENT_STARTED,
            document=document.value,
            format=format.value,
            character_count=len(markdown),
        )

        rendered = await self._render_bounded(
            markdown, document, format, started_at, timeout_seconds
        )
        self._refuse_oversized_output(rendered, format, started_at)

        log.info(
            _EVENT_SUCCEEDED,
            format=format.value,
            byte_size=len(rendered),
            duration_ms=_elapsed_ms(started_at),
        )
        return rendered

    async def _render_bounded(
        self,
        markdown: str,
        document: TailoredDocumentKind,
        format: ExportFormat,
        started_at: float,
        timeout_seconds: int,
    ) -> bytes:
        """The thread, the deadline, and the three `except` clauses, in the order they must be in.

        `asyncio.CancelledError` matches none of them and is therefore not caught — it is a
        `BaseException`, and a worker shutting down is not a render failure (X-36).
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._render_in_thread, markdown, document, format),
                timeout_seconds,
            )
        except TimeoutError as exc:
            # `wait_for` cancels the *await*; the thread running the synchronous library keeps going
            # until it returns or the task's hard time limit recycles the child. That is why the
            # stale window has to sit above the hard limit, and why `create_celery` refuses to start
            # when it does not (AC-20, X-26).
            self._log_failure(
                "render_timed_out", format, exc, started_at, timeout_seconds=timeout_seconds
            )
            raise DocumentRenderTimedOut() from None
        except _DOCUMENT_ERRORS as exc:
            self._log_failure("render_failed", format, exc, started_at)
            raise DocumentRenderFailedOnDocument() from None
        except Exception as exc:
            # **THE FLOOR** (X-37). Four libraries sit under this one port and the surface beneath it
            # is every way any of them can fail on a document a stranger wrote. This is what makes
            # `DocumentRendererPort`'s promise true by construction; the clause above is only ever
            # an improvement on the reason, never the thing that keeps the promise.
            self._log_failure("render_error", format, exc, started_at)
            raise DocumentRenderError() from None

    def _render_in_thread(
        self, markdown: str, document: TailoredDocumentKind, format: ExportFormat
    ) -> bytes:
        """The `match` that is the whole of this adapter's per-format logic (ADR-0017 §1).

        Closed by `assert_never`, so a fifth `ExportFormat` stops `mypy --strict` here with the
        member's name rather than falling through to a branch that happens to be nearby.
        """
        match format:
            case ExportFormat.MD:
                # No parse. The stored Markdown **is** the export — re-rendering it through the
                # pipeline could only change it, and "the source, exactly as saved" is the promise
                # this format makes (ADR-0017 §3).
                return markdown.encode("utf-8")
            case ExportFormat.TXT:
                return render_plain_text(self._tokens(markdown)).encode("utf-8")
            case ExportFormat.DOCX:
                return render_docx(self._tokens(markdown), document)
            case ExportFormat.PDF:
                fragment = render_body_fragment(self._tokens(markdown))
                # **Sanitize the fragment, then wrap it.** Not `sanitize(render_html(...))`, which
                # was measured at I3 to delete the document shell and keep the title's *text* — a
                # stray line reading "Tailored CV" above the name in every PDF, because `html`,
                # `head`, `title` and `body` are (correctly) not on the grammar's allow-list. The
                # technical plan's step 3 is corrected in place; `html.render_body_fragment` carries
                # the full account.
                html = wrap_in_document(self._sanitize(fragment), document)
                return render_pdf(html, url_fetcher=self._url_fetcher)
            case _:
                assert_never(format)

    def _tokens(self, markdown: str) -> Sequence[Token]:
        """Parse, then normalize. The three walkers only ever see the output of this (ADR-0017 §2)."""
        return normalize_to_grammar(parse_document(markdown))

    def _timeout_for(self, format: ExportFormat) -> int:
        """5 s inside a request, 60 s in the worker — chosen by the format, never by the caller.

        A `match` over `ExportDelivery` closed by `assert_never`, rather than an argument or an
        `if format in (...)`: the caller knowing which bound applies would mean the API could hand
        the worker's 60 seconds to a render happening inside an HTTP request, and that number is the
        difference between a slow response and a stalled event loop.
        """
        match format.delivery:
            case ExportDelivery.INLINE:
                return self._settings.export_inline_timeout_seconds
            case ExportDelivery.QUEUED:
                return self._settings.export_render_timeout_seconds
            case _:
                assert_never(format.delivery)

    def _refuse_oversized_output(
        self, rendered: bytes, format: ExportFormat, started_at: float
    ) -> None:
        """X-27, checked on the **produced bytes** — the only number here that is a fact.

        Nothing is written and nothing is truncated: half a PDF on the shared uploads volume is a
        file the user can download and cannot open, and one whose size nothing bounds is space
        somebody else's document was going to need.
        """
        limit = self._settings.export_max_file_bytes
        if len(rendered) <= limit:
            return
        # Its own `log.warning` rather than `_log_failure`, because there is no exception here: the
        # render succeeded and we are refusing what it produced. `error_type=None` keeps the four
        # `export.render_failed` lines one shape, so a query over the event does not have to know
        # which reason omits which key.
        log.warning(
            _EVENT_FAILED,
            format=format.value,
            reason="output_too_large",
            error_type=None,
            duration_ms=_elapsed_ms(started_at),
            byte_size=len(rendered),
            limit=limit,
        )
        raise DocumentRenderOutputTooLarge()

    def _log_failure(
        self,
        reason: str,
        format: ExportFormat,
        exc: BaseException,
        started_at: float,
        **extra: object,
    ) -> None:
        """The one `export.render_failed` shape, and the one place the privacy rule is enforced.

        `error_type` is a class name and can carry no document content. `str(exc)` and `exc_info` are
        absent by design: WeasyPrint's messages quote the CSS declaration and the HTML that produced
        them, and `python-docx`'s quote XML — both built out of somebody's employment history.
        """
        log.warning(
            _EVENT_FAILED,
            format=format.value,
            reason=reason,
            error_type=_qualified_type(exc),
            duration_ms=_elapsed_ms(started_at),
            **extra,
        )


def _qualified_type(exc: BaseException) -> str:
    """`module.QualName` — the only thing about an exception this module is allowed to log."""
    return f"{type(exc).__module__}.{type(exc).__qualname__}"


def _elapsed_ms(started_at: float) -> int:
    """Milliseconds since `started_at`, from `time.perf_counter()`.

    `perf_counter` and not the `Clock` port, for `GeminiLlm._elapsed_ms`' reason: the port is
    whole-second by contract (ADR-0007), which would make a subtraction accurate to ±1 s on a render
    measured in tens of milliseconds. This measures a DURATION, never a "now"; it cannot date
    anything and never reaches an aggregate's timestamp field.
    """
    return round((time.perf_counter() - started_at) * 1000)


if TYPE_CHECKING:
    # Makes mypy prove this satisfies the port structurally rather than by eye.
    def _assert_implements_document_renderer_port(adapter: MarkdownDocumentRenderer) -> None:
        _: DocumentRendererPort = adapter
