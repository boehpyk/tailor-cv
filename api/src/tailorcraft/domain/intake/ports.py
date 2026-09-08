"""Ports the `intake` context needs from the outside world, in the domain's own language.

Both protocols below name no library, no HTTP detail, no filesystem detail, no retry count and no
timeout — that is adapter business (ADR-0004). `BaseCvRepository` is implemented in
`infrastructure/persistence/repositories/intake/base_cv.py`; `CvTextExtractorPort` in
`infrastructure/intake/extraction.py`. Neither module is imported here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType, ExtractedText


class BaseCvRepository(Protocol):
    """Persistence for the `BaseCv` aggregate.

    `get` raises `BaseCvNotFound` rather than returning `None`, while `GuestSessionRepository.
    find_by_token_hash` (`domain/identity/ports.py`) returns `GuestSession | None`. That asymmetry is
    deliberate, not an inconsistency to "fix": `get` is called where the caller already believes the
    row exists (a use case that was handed an id it minted, or one it is authorized to look up), so an
    absence there is exceptional and gets raised. `find_by_token_hash` is called on every request with
    whatever cookie happened to arrive, where "no such session" is an entirely ordinary outcome the
    caller must branch on — raising there would turn "not logged in yet" into an exception-handling
    concern for every route.
    """

    def next_identity(self) -> BaseCvId:
        """Mint an id for a `BaseCv` that does not exist yet. Synchronous: identity assignment is
        application-side (UUIDv7, ADR-0007) and needs no I/O, unlike every other method here."""
        ...

    async def add(self, cv: BaseCv) -> None: ...

    async def get(self, cv_id: BaseCvId) -> BaseCv:
        """Raises `BaseCvNotFound` if no `BaseCv` with this id exists. Does **not** check ownership —
        that is `BaseCvNotOwnedBySession`, a use-case-level decision (F-20), not this port's job."""
        ...

    async def list_for_session(self, sid: GuestSessionId) -> Sequence[BaseCv]:
        """Every `BaseCv` owned by `sid`, for `GET /api/base-cvs`. An empty sequence when the session
        owns none — never an error; an empty list is a perfectly ordinary answer to this question."""
        ...

    async def count_for_session(self, sid: GuestSessionId) -> int:
        """How many base CVs `sid` owns, for the `TooManyBaseCvs` check (F-23). A separate method
        from `list_for_session` rather than `len(await list_for_session(sid))` so the SQL adapter can
        answer with `COUNT(*)` instead of materializing every row just to measure them."""
        ...


class CvTextExtractorPort(Protocol):
    """Pulls text out of an uploaded file's bytes.

    Takes `bytes` rather than a `FileRef`, on purpose: that keeps this port single-purpose (parse
    bytes into text — nothing about where they came from) and testable from a plain byte fixture, with
    no `FileStorePort` or file system anywhere in the test. Holding the whole file in memory to do
    that is acceptable specifically *because* the 10 MB upload cap (`settings.max_upload_bytes`)
    already bounds how large `data` can ever be — a link that is not obvious from this signature
    alone, which is why it is written down here rather than left for a future reader to wonder about.
    """

    async def extract(self, content_type: CvContentType, data: bytes) -> ExtractedText:
        """Raises a `CvExtractionFailed` subclass (`domain/intake/errors.py`) on every failure the
        use case must translate into `BaseCv.mark_extraction_failed` — never a bare exception from
        whatever library the adapter happens to use underneath."""
        ...
