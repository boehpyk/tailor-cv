"""The `RenderDocumentInline` use case: render one of a run's current documents into an **inline**
format (`md` or `txt`) inside the request, and hand the bytes straight back.

The whole of ADR-0016 (a) in one module: *TXT and Markdown are representations, not jobs.* There is
no aggregate here, no row, no file, no worker and no transaction — which is why this is the only
use case in the codebase that publishes a domain event that no aggregate recorded, and why that is
safe. See `DocumentRenderedInline`'s own docstring for the argument.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRunForSession
from tailorcraft.domain.export.ports import DocumentRendererPort
from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId


@dataclass(frozen=True, slots=True)
class RenderDocumentInlineCommand:
    """Who is asking, which run, which document, and which inline format.

    The same four fields as `RequestExportCommand`, and the two are still two types. They are the
    contracts of two different user intents that happen to be parameterized alike — *give me this
    now* and *prepare this for me* — and a shared command would have to be widened the moment
    either grew a field the other has no meaning for. Shared shape is not shared behaviour
    (CLAUDE.md); a base class here would guess at rules that already differ, since `format` means
    the inline half of the enum in one and the queued half in the other.

    `format` **must be an inline member**; `ExportFormatNotInline` otherwise (X-1). The HTTP
    boundary narrows the query parameter to `md` / `txt` with a literal type, so this command is
    the second lock behind it rather than the first — and the use case's own guard is the one a
    future non-HTTP caller cannot route around.
    """

    guest_session_id: GuestSessionId
    tailoring_run_id: TailoringRunId
    document: TailoredDocumentKind
    format: ExportFormat


@dataclass(frozen=True, slots=True)
class RenderedInlineDocument:
    """The bytes, plus the two facts the router needs to build its headers.

    `format` and `document` are echoed back rather than re-read off the command by the router,
    because the response's `Content-Type`, its `Content-Disposition` filename and the
    `DocumentRenderedInline` event all have to agree about which document in which format these
    bytes are, and one answer beats three. `download_filename(document, format)` is the domain
    function that turns the pair into the name (AC-8).

    `data: bytes`, never `str`, even for `md` and `txt` — the boundary writes bytes, the byte count
    is what the event carries, and a `str` here would leave the encoding to be decided at whichever
    layer happened to serialize last. UTF-8 is chosen once, in the renderer.

    There is no `byte_size` field: it is `len(data)`, and a stored copy of a derivable number is a
    second source of truth waiting to disagree.
    """

    data: bytes
    format: ExportFormat
    document: TailoredDocumentKind


class RenderDocumentInline:
    """Render a run's current CV or cover letter to Markdown or plain text, inside the request.

    **No repository, no save, no transaction of its own, and no `ExportJob`.** The bytes are a
    *representation* of something that already exists, not an artifact anyone waits for: rendering
    Markdown is string manipulation, and rendering plain text is a walk over a token stream
    (ADR-0005's rule is the cost of the work, not the tidiness of treating all four formats alike).
    A row here would be a record of a `GET` — a fact nobody reads, on a table the 24-hour purge
    then has to carry.

    It composes `GetTailoringRunForSession` for the same reason `RequestExport` does: the
    authorization rule and the 404 collapse are that use case's, inherited rather than written a
    fifth time. This is the *third* entry point onto a `TailoringRun` in this slice alone, which is
    the argument for centralizing the check making itself.

    Flow (technical-plan.md, "Application layer" §3; T7 implements it):

    1. ``run = await get_tailoring_run(cmd.tailoring_run_id, cmd.guest_session_id)`` —
       `GuestSessionExpired` (X-2), `TailoringRunNotFound` for both "absent" and "not mine" (X-3).
    2. ``if run.status is not TailoringRunStatus.SUCCEEDED: raise TailoringRunNotExportable(
       run.status)`` (X-4). The aggregate's own status, as in `RequestExport` step 2.
    3. ``if cmd.format.delivery is not ExportDelivery.INLINE: raise ExportFormatNotInline(
       cmd.format)`` (X-1). It asks `format.delivery`, never ``cmd.format in (MD, TXT)``: "which
       formats are inline" is `ExportFormat`'s fact to hold (AC-1), and a membership test written
       out here would be a second copy of it that a fifth format could walk straight past.
    4. ``docs = run.current_documents`` — narrowed for `mypy`; ``source = docs.cv if cmd.document
       is CV else docs.cover_letter``. **`current_documents` is the revision if one exists, else
       the draft** (ADR-0015 §1, X-8), which is the whole of that branch: this use case does not
       choose, it reads the one the aggregate calls current.
    5. ``data = await renderer.render(source.value, document=cmd.document, format=cmd.format)``.
    6. ``await events.publish(DocumentRenderedInline(...))``; return `RenderedInlineDocument`.

    **`DocumentRenderFailed` propagates here, and that is the deliberate opposite of
    `RenderExportJob`**, which catches the identical exception family and records it on the job.
    ADR-0014 §2 again — *was anything spent, and is there an artifact to own?* On this path,
    nothing and none: no money, no worker second, no row, no file. A failure has nowhere to be
    recorded and nothing to be recorded *on*, so manufacturing a row to hold it would be the shape
    ADR-0013 rejected for a failed fetch. The router maps the two reasons the boundary can see:
    `DocumentRenderError` → **500** `render_failed` (our bug, the one deliberate 500 in the
    codebase, X-5) and `DocumentRenderTimedOut` → **503** `render_timed_out` (X-6).

    **Step 6 publishes an event no aggregate recorded**, which is a deliberate exception to "events
    are released from an aggregate after a successful save" and is safe for exactly the reason that
    rule exists: the rule protects against announcing a fact a failed transaction is about to
    un-happen, and here there is no transaction to fail. By the time this line runs the bytes exist
    and are about to be returned. `DocumentRenderedInline` is the **cost-and-size line for a path
    that has no row at all** — without it, half this slice's traffic is invisible to the dashboard
    watching the other half. Its payload carries `byte_size` and **never the text**, which on this
    path is the entire response body and therefore the easiest thing in the slice to attach by
    accident.

    **Publish after the render, never before.** A publish on the way in would announce a rendering
    that a `DocumentRenderFailed` is about to make untrue, and since nothing here rolls back,
    nothing would take it back.
    """

    def __init__(
        self,
        get_tailoring_run: GetTailoringRunForSession,
        renderer: DocumentRendererPort,
        events: EventPublisherPort,
    ) -> None:
        self._get_tailoring_run = get_tailoring_run
        self._renderer = renderer
        self._events = events

    async def __call__(self, cmd: RenderDocumentInlineCommand) -> RenderedInlineDocument:
        raise NotImplementedError
