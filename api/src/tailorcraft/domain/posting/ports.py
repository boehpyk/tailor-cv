"""Ports the `posting` context needs from the outside world, in the domain's own language.

Neither protocol below names a library, an HTTP detail, a timeout, a retry count or a status code —
that is adapter business (ADR-0004). `JobPostingRepository` is implemented in
`infrastructure/persistence/repositories/posting/job_posting.py`; `JobPostingFetcherPort` in
`infrastructure/posting/fetching.py`. Neither module is imported here, and the dependency only ever
points this way.

**These get no red-first cycle, and the reason is worth stating rather than looking like an
exemption** (docs/sdlc.md §2). A `Protocol` has no behaviour: every method body is `...`, so there
is nothing that could fail an assertion, and a "test" of one would either assert that Python still
has ellipses or silently test whichever adapter it imported to stand in. What proves these are right
is that the adapters satisfy them — each carries an `if TYPE_CHECKING:` structural-conformance
assertion that makes `mypy --strict` do the checking — and that the use cases compile against them.
Verification here is by inspection, and the thing to inspect is in the next paragraph.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import FetchedPosting, JobPostingId, SourceUrl


class JobPostingRepository(Protocol):
    """Persistence for the `JobPosting` aggregate.

    Deliberately the same shape as `BaseCvRepository` (`domain/intake/ports.py`), including the
    `get`-raises / `find`-returns-`None` asymmetry documented there: `get` is called where the caller
    already believes the row exists — a use case holding an id it minted, or one it is authorized to
    look up — so an absence is exceptional and gets raised, while a lookup on whatever cookie
    happened to arrive is an ordinary branch and returns `None`.
    """

    def next_identity(self) -> JobPostingId:
        """Mint an id for a `JobPosting` that does not exist yet.

        Synchronous, unlike everything else here: identity is application-assigned (UUIDv7,
        ADR-0007) and needs no I/O. That is what lets the aggregate be fully valid before it ever
        meets the database, which in turn is what makes the domain tests in `tests/unit/posting/`
        possible without one.
        """
        ...

    async def add(self, posting: JobPosting) -> None: ...

    async def get(self, posting_id: JobPostingId) -> JobPosting:
        """Raises `JobPostingNotFound` if no `JobPosting` with this id exists.

        Does **not** check ownership. That is `JobPostingNotOwnedBySession`, a use-case decision
        (P-30/AC-14) — a repository that silently filtered by session would make the authorization
        rule invisible at the call site, and invisible rules are the ones a second entry point
        forgets.
        """
        ...

    async def list_for_session(self, sid: GuestSessionId) -> Sequence[JobPosting]:
        """Every `JobPosting` owned by `sid`, for `GET /api/job-postings`. An empty sequence when the
        session owns none — never an error; "you have captured nothing yet" is an ordinary answer."""
        ...

    async def count_for_session(self, sid: GuestSessionId) -> int:
        """How many postings `sid` owns, for the `TooManyJobPostings` check (P-32).

        A separate method rather than `len(await list_for_session(sid))` so the SQL adapter can
        answer with `COUNT(*)` instead of materializing every row — including every row's full
        posting text — just to measure how many there are.
        """
        ...


class JobPostingFetcherPort(Protocol):
    """Retrieve a job posting from a URL the visitor chose.

    **Look at what this signature does not say.** No `httpx`, no `trafilatura`, no timeout, no
    redirect count, no byte cap, no User-Agent, no `Response`, no status code, no header, no proxy,
    no address policy. Every one of those is real and load-bearing — ADR-0012 spends ten obligations
    on them — and every one of them lives in the adapter. If any of them appeared here it would be a
    rename rather than a port: the domain would have opinions about HTTP, and swapping the transport
    would become a change to the domain (ADR-0004).

    What the domain does care about is exactly this: give it a URL it considers well-formed, and get
    back text it considers usable, or a failure it has a name for.
    """

    async def fetch(self, url: SourceUrl) -> FetchedPosting:
        """Fetch and extract one job posting.

        Raises a `JobPostingFetchFailed` subclass (`domain/posting/errors.py`) on **every** failure —
        never a bare `httpx`, `lxml` or `socket` exception. The adapter guarantees that structurally
        with an `except Exception` floor beneath its specific translations rather than with an
        allow-list of the library errors it happened to think of, because an allow-list is a bet
        that you enumerated every way a vendor library can fail on a page a stranger chose, and that
        bet loses (ADR-0012 obligation 10; the same lesson CLAUDE.md records for the CV extractor).

        Unlike `CvTextExtractorPort.extract`, whose failures the use case catches and records as a
        state of the aggregate, **these propagate all the way to the router** — a failed fetch
        produces no `JobPosting` at all (ADR-0013). The use case does not catch them, and an
        application test asserts that it does not.
        """
        ...
