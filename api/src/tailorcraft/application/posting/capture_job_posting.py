"""The `CaptureJobPosting` use case: accept a job description, pasted or fetched.

One use case, two commands, one `__call__` with a `match` — deliberately not two use case classes.
The two paths differ in exactly one step (where the text comes from) and agree on five: resolve the
session, check it has not expired, check the per-session cap, save, publish. Splitting them would
duplicate all five, and *a check duplicated in every caller is a check one caller eventually
forgets* — the argument `GetBaseCvForSession`'s docstring already makes. A `match` over a union of
frozen dataclasses expresses "one intent, two shapes of input" exactly, and it is this codebase's
first structural pattern match (Constitution §2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from tailorcraft.domain.identity.errors import GuestSessionExpired
from tailorcraft.domain.identity.ports import GuestSessionRepository
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.posting.errors import TooManyJobPostings
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.ports import JobPostingFetcherPort, JobPostingRepository
from tailorcraft.domain.posting.value_objects import (
    JobPostingId,
    JobPostingText,
    PostingSource,
    PostingTitle,
    SourceUrl,
)
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort


@dataclass(frozen=True, slots=True)
class PasteJobPostingCommand:
    """The visitor pasted the description in.

    `text` arrives as a `JobPostingText`, already validated: the boundary builds the value object
    and translates its `DomainError` into a 422, so by the time a command exists the length rules
    have been enforced and this use case never re-checks them.
    """

    guest_session_id: GuestSessionId
    text: JobPostingText


@dataclass(frozen=True, slots=True)
class FetchJobPostingCommand:
    """The visitor gave us a link to fetch.

    `url` is a `SourceUrl`, so the scheme allow-list and the rest of ADR-0012's obligation 1 have
    already been applied at the boundary — this use case cannot be handed a `file://` URL because
    no such value exists.
    """

    guest_session_id: GuestSessionId
    url: SourceUrl


CaptureJobPostingCommand = PasteJobPostingCommand | FetchJobPostingCommand
"""The tagged union the use case matches over. The wire-format counterpart is Pydantic's
`Field(discriminator="source")` in `infrastructure/api/schemas/posting.py` — the same shape at both
boundaries, on purpose (ADR-0013)."""


@dataclass(frozen=True, slots=True)
class CaptureJobPostingResult:
    """What the caller gets back — ids and counts, never the aggregate itself.

    No `status` field, and the absence is the point: a `JobPosting` that exists is complete
    (ADR-0013). Contrast `UploadBaseCvResult`, which must carry a `status` and a `failure_reason`
    because a `BaseCv` can legitimately exist in a failed state.
    """

    job_posting_id: JobPostingId
    source: PostingSource
    character_count: int
    title: PostingTitle | None


class CaptureJobPosting:
    """Capture one job posting for a guest session, from pasted text or from a fetched URL.

    Flow (technical-plan.md, "Flow"):

    1. ``session = await sessions.get(cmd.guest_session_id)`` — raises `GuestSessionNotFound`.
    2. ``if session.is_expired(clock.now()): raise GuestSessionExpired``.
    3. ``if await postings.count_for_session(session.id) >= max_per_session: raise
       TooManyJobPostings`` — a **cross-aggregate policy**, deliberately not an invariant of
       `JobPosting`: the rule spans every posting a session owns, a fact no single instance has
       access to. Soft cap; concurrent requests may overshoot by the number in flight, accepted and
       documented rather than locked, exactly as 1.1's `TooManyBaseCvs` is.
    4. ``posting_id = postings.next_identity()``.
    5. ``match cmd:`` — the pasted arm builds from `cmd.text`; the fetched arm awaits
       ``fetcher.fetch(cmd.url)`` first. **`JobPostingFetchFailed` is NOT caught here** — see the
       comment at that line in `__call__`.
    6. ``await postings.add(posting)``.
    7. ``await events.publish(*posting.release_events())`` — after the save, never before.
    8. Return `CaptureJobPostingResult`.

    **Note an absence, so it does not read as an oversight.** `UploadBaseCv`'s step 5 writes a file
    *before* the row exists and carries a long comment about the crash window it chose (ADR-0006
    §2). There is no analogue here: everything this use case writes goes into one database
    transaction, so a rollback leaves nothing behind — no file, no cache entry, no queue row, and
    therefore nothing for 1.6's orphan sweep to find from this slice.
    """

    def __init__(
        self,
        postings: JobPostingRepository,
        sessions: GuestSessionRepository,
        fetcher: JobPostingFetcherPort,
        events: EventPublisherPort,
        clock: Clock,
        max_per_session: int = 10,
    ) -> None:
        self._postings = postings
        self._sessions = sessions
        self._fetcher = fetcher
        self._events = events
        self._clock = clock
        self._max_per_session = max_per_session

    async def __call__(self, cmd: CaptureJobPostingCommand) -> CaptureJobPostingResult:
        session = await self._sessions.get(cmd.guest_session_id)
        if session.is_expired(self._clock.now()):
            raise GuestSessionExpired(str(session.id))

        # Cross-aggregate policy, deliberately not an invariant of `JobPosting`: the rule spans
        # every posting a session owns, a fact no single instance has access to. A soft cap —
        # concurrent requests can overshoot it by the number in flight, which is accepted and
        # documented rather than locked, exactly as `TooManyBaseCvs` is (P-32).
        if await self._postings.count_for_session(session.id) >= self._max_per_session:
            raise TooManyJobPostings(str(session.id))

        posting_id = self._postings.next_identity()
        created_at = self._clock.now()

        # The codebase's first structural pattern match, and the reason this is one use case rather
        # than two: everything above and below is shared, and only this block differs.
        match cmd:
            case PasteJobPostingCommand():
                posting = JobPosting.from_pasted_text(
                    id=posting_id,
                    guest_session_id=session.id,
                    text=cmd.text,
                    created_at=created_at,
                )
            case FetchJobPostingCommand():
                # `JobPostingFetchFailed` is deliberately NOT caught here, and this is the one line
                # in the file most likely to be "fixed" by someone who has just read `UploadBaseCv`.
                #
                # That use case wraps `extractor.extract` in `try/except CvExtractionFailed` and
                # turns a failure into `cv.mark_extraction_failed(...)` — a recorded state of an
                # aggregate that exists either way. Doing the same here would be wrong, because the
                # two situations differ in the thing that matters: a failed extraction still leaves
                # a `BaseCv` row that is the receipt for bytes sitting on a volume, while a failed
                # fetch holds nothing at all — no bytes, no text, no file. A row here would record
                # an *event*, and this codebase has domain events for that.
                #
                # It also protects slice 1.3: "a `JobPosting` always has usable text" is what lets
                # the tailoring use case skip a `None` check that would otherwise be in every
                # consumer forever. See ADR-0013. `qa` asserts this propagation over all nine
                # subclasses, so the "fix" turns nine tests red rather than passing silently.
                fetched = await self._fetcher.fetch(cmd.url)
                posting = JobPosting.from_fetched_url(
                    id=posting_id,
                    guest_session_id=session.id,
                    url=cmd.url,
                    fetched=fetched,
                    created_at=created_at,
                )
            case _:  # pragma: no cover — unreachable while the union has exactly two members
                # `assert_never` is what turns "I handled every case" from a claim into a check.
                # mypy narrows `cmd` to `Never` here only if the two cases above are exhaustive; add
                # a third command to `CaptureJobPostingCommand` without a `case` for it and this
                # line becomes a type error naming the type that was missed. Without it, the same
                # mistake is an `UnboundLocalError` at runtime, on whichever request happens to use
                # the new command — and mypy's `possibly-undefined` already flagged exactly that
                # gap here, which is how this line came to be written.
                assert_never(cmd)

        await self._postings.add(posting)

        # Released and published only after the aggregate is saved — never before, so a publish
        # never announces a fact that a failed save is about to un-happen.
        await self._events.publish(*posting.release_events())

        return CaptureJobPostingResult(
            job_posting_id=posting.id,
            source=posting.source,
            character_count=posting.text.character_count,
            title=posting.title,
        )


__all__ = [
    "CaptureJobPosting",
    "CaptureJobPostingCommand",
    "CaptureJobPostingResult",
    "FetchJobPostingCommand",
    "PasteJobPostingCommand",
]
