"""Value objects for the `export` context: what a format is, where a job stands, and why it failed.

The same rule `intake`, `posting` and `tailoring` follow: a frozen `@dataclass(frozen=True,
slots=True)` where there is something to validate, a `StrEnum` where the set is closed. Never
Pydantic (ADR-0002).

One of the six declarations here validates nothing and the other five are closed enums, so — as in
`posting` — most of this module has nothing to defer to a GREEN step. What the skeleton exists for
is the three `ExportFormat` properties and `download_filename`: four functions whose *bodies* are
the twelve facts of AC-1 and the eight constants of AC-27, and whose signatures have to exist
before `qa` can watch an assertion about them fail rather than an `ImportError` (docs/sdlc.md §2).

**This module never imports `domain/shared/files.py`**, and the direction is load-bearing rather
than incidental: `files.py` imports `ExportJobId` and `ExportFormat` from *here*, so that
`FileRef.for_export` can exist beside `for_base_cv`. An import back the other way would close the
loop and turn `ExportJob`'s own import of `FileRef` into a cycle. `domain/export/__init__.py`'s
docstring carries the full account.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind


@dataclass(frozen=True, slots=True)
class ExportJobId:
    """An `ExportJob`'s identity, typed so a signature cannot silently accept the wrong UUID.

    The reason `TailoringRunId` gives, applied to a context that holds four foreign ids at once. The
    download and poll handlers both compare a job's `guest_session_id` to the resolved session id,
    and both take a job id out of the URL beside a run id that is also in scope; with bare `UUID`s
    everywhere, handing a *run* id to `jobs.get(...)` is a lookup that quietly finds nothing instead
    of a `mypy --strict` error before the code ever runs.
    """

    value: UUID

    # No `__post_init__` here on purpose: every `UUID` is already a valid `ExportJobId`, so a
    # validation method that does nothing would just be a place a future reader adds a rule that
    # does not belong. `TailoringRunId`, `BaseCvId`, `JobPostingId` and `GuestSessionId` are the
    # same shape for the same reason, and this type is therefore complete as written — there is
    # nothing here for the RED step to turn red.


class ExportDelivery(StrEnum):
    """How a format gets to the user: inside the request, or through a worker and a poll.

    Two members, closed, and named so that a `match` over one is **exhaustive with
    `assert_never`** in both use cases and in the router that branches on it. That is the whole
    reason this is a type rather than an `is_inline: bool` on `ExportFormat`: a boolean makes the
    two branches of every `if` look like a style choice, while a `match` over a two-member enum
    with an `assert_never` default fails to type-check the day a third delivery appears. ADR-0005's
    rule — *the cost of the work, not the tidiness of treating all four alike* — is the thing being
    modelled, and it deserves a name.
    """

    INLINE = "inline"
    QUEUED = "queued"


class ExportFormat(StrEnum):
    """One of the four formats a document can leave in: `md`, `txt`, `pdf`, `docx`.

    **An enum, not a frozen dataclass, and the reason belongs here rather than in a review
    comment.** The set is closed — four members, fixed by ADR-0017's pipeline, not parsed out of
    anything a stranger sends. There is nothing a `__post_init__` could validate, because there is
    no such thing as a malformed member. And the string values are the **wire spellings**: a query
    parameter (`?format=pdf`), a JSON field on the job resource and a file extension in a storage
    key all use `pdf`, with no mapping table anywhere that could drift out of step with this one.
    A frozen dataclass would buy exactly one capability — constructing a fifth format at runtime —
    which is precisely the thing nobody should be able to do.

    The three properties below are where the *facts about* a format live, so that the questions
    they answer are answered once, on the type:

    - `delivery` — is rendering this format string manipulation (inside the request) or CPU-bound
      and synchronous (a job, a worker, a file)? **No router, use case or React component
      re-derives this** (AC-1). A `to_thread` around WeasyPrint inside a route would pass every
      test and collapse at five concurrent users, and the defence against writing one is that the
      answer is not available to be guessed at.
    - `media_type` — the `Content-Type` two different routers and one storage grammar must agree
      about. A constant in the inline router and another in the file router is two constants that
      eventually disagree.
    - `file_extension` — the member's own value, which is what makes ADR-0011's grammar and the
      wire spelling the same string by construction rather than by convention.
    """

    MD = "md"
    TXT = "txt"
    PDF = "pdf"
    DOCX = "docx"

    @property
    def delivery(self) -> ExportDelivery:
        """`INLINE` for `md` and `txt`, `QUEUED` for `pdf` and `docx` (AC-1)."""
        raise NotImplementedError

    @property
    def media_type(self) -> str:
        """The `Content-Type` this format is served as, charset included where it applies."""
        raise NotImplementedError

    @property
    def file_extension(self) -> str:
        """The extension a file of this format carries — the member's own value."""
        raise NotImplementedError


class ExportJobStatus(StrEnum):
    """Where an `ExportJob` stands: `queued` → `rendering` → `ready` | `failed`.

    Four values, and the twelve-cell transition table between them (AC-3) is the aggregate's whole
    behaviour. An enum for the reason `TailoringRunStatus` is one: the set is closed and the
    boundary never parses one out of user input, so a `__post_init__` would have nothing to check.

    **Deliberately not `running` and not `succeeded`, although the shape mirrors
    `TailoringRunStatus` exactly.** A render is not a run: it spends worker seconds rather than
    money, it produces a file rather than two documents, and a reader grepping the logs or the
    database for `running` should find tailoring and only tailoring. Two state machines with
    identical member names would make every log query and every dashboard filter ambiguous, and
    the ambiguity would only be noticed by someone reading a number that is wrong. `rendering`
    also says what the worker is actually doing, which `running` does not.
    """

    QUEUED = "queued"
    RENDERING = "rendering"
    READY = "ready"
    FAILED = "failed"


class ExportFailureReason(StrEnum):
    """Why an `ExportJob` ended in `failed`. Nine values, closed.

    **Persisted**, for ADR-0014 §2's reason with the money replaced by worker time and a file: a
    job that reached a worker spent seconds of a machine and may have left bytes on the volume, so
    every outcome is a row, `ready` and `failed` alike. (Contrast `FetchFailureReason`, which is not
    persisted, because nothing was spent and there is no artifact to own.)

    Four of the nine are raised by `DocumentRendererPort` as a named `DocumentRenderFailed`
    subclass — `RENDER_FAILED`, `RENDER_TIMED_OUT`, `OUTPUT_TOO_LARGE` and `RENDER_ERROR`, the
    residual reached by the adapter's `except Exception` floor — and a fifth,
    `FILE_STORE_UNAVAILABLE`, is raised by `FileStorePort`.

    **Four have no exception subclass at all, and the asymmetry is the point:
    `SOURCE_CHANGED`, `SOURCE_UNAVAILABLE`, `NOT_QUEUED` and `ABANDONED` are produced by our own
    orchestration, not by a port.** Nothing outside this process can raise them, so inventing a
    `SourceChanged(DocumentRenderFailed)` would be an exception type that no `raise` statement ever
    mentions. `SOURCE_CHANGED` is written when the run's version moved between the click and the
    render (X-31); `SOURCE_UNAVAILABLE` when the run is no longer `succeeded` (X-35); `NOT_QUEUED`
    when the row committed and the broker then refused the publish (X-22); `ABANDONED` when a job
    has been `rendering` past the stale window and nobody is coming back for it (X-29).
    `TailoringFailureReason` splits the same way for the same reason, two of its nine.
    """

    RENDER_FAILED = "render_failed"
    RENDER_TIMED_OUT = "render_timed_out"
    OUTPUT_TOO_LARGE = "output_too_large"
    FILE_STORE_UNAVAILABLE = "file_store_unavailable"
    SOURCE_CHANGED = "source_changed"
    SOURCE_UNAVAILABLE = "source_unavailable"
    NOT_QUEUED = "not_queued"
    ABANDONED = "abandoned"
    RENDER_ERROR = "render_error"


def download_filename(document: TailoredDocumentKind, format: ExportFormat) -> str:
    """The **constant** filename a download is offered under: one of eight, keyed on (document,
    format) — `tailored-cv.pdf`, `cover-letter.docx`, and the six others.

    **A pure function in the domain rather than an `ExportFilename` value object**, because there
    is no value here with rules of its own to protect: nothing ever parses a filename back in, and
    a type whose only job is to hold the output of a `match` is a type with no invariant. A
    function is the honest shape.

    **In the domain rather than in the router**, for two reasons. The first is mundane: the inline
    handler and the file handler both need it, and two copies of a mapping table are two tables
    that eventually disagree — the same argument that put `media_type` on `ExportFormat`. The
    second is the one that matters: *the filename never derives from user text* is a **rule**, not
    a formatting choice (AC-27). A tailored CV's first line is whatever the model wrote from
    whatever the user uploaded, and a `Content-Disposition` assembled from it is a header an
    attacker gets to write — AC-27's test exports a document beginning `"; filename="evil.exe` and
    asserts the header is byte-identical to the constant. A rule about what may not influence an
    output belongs where rules live, not in the layer that happens to emit the header.

    T3 implements this as a `match` over both enums with an `assert_never` default, so that a fifth
    format or a third document kind is a type error here before it is a missing filename in
    production.
    """
    raise NotImplementedError
