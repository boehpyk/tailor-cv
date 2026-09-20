"""The purge's two data-access wrappers, shared by every root that runs a purge.

Both classes lived in `infrastructure/tasks/container.py` until the CLI needed them (slice 1.6,
T23). They moved here rather than being copied, and the reason is the thing they encode:
`CommittingExpiredGuestDataAdapter` is where *"rows first, **committed**, then files"* stops being a
sentence and becomes a transaction boundary, and the CLI deletes exactly the same stranger's data by
exactly the same rule. Two copies of a durability rule is two copies that can drift, and the one
that drifts is the one nobody runs on a schedule.

**Why the CLI could not simply import `tasks/container.py`.** That module builds the tailoring root,
so it imports `GeminiLlm` at its top, which imports the Google SDK. `purge-guests` publishes no
task, talks to no model and has no reason to load one — the same argument `cli.py` already makes for
lazy-importing the eval runner, pointing the other way. `tasks/container.py` imports both names from
here, so the worker's root reads exactly as it did before.

Neither class is a domain port: they are persistence-boundary decisions (a transaction per session)
and an entry-point question (how much is still overdue), which is why they sit in `infrastructure/`
beside the lock and the heartbeat rather than in `domain/retention/ports.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.retention.ports import ExpiredGuestDataPort
from tailorcraft.domain.retention.value_objects import ExpiringGuestSession, RetentionWindow
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.files import FileRef


class CommittingExpiredGuestDataAdapter:
    """The purge's `ExpiredGuestDataPort`: the ordinary one, except that **`delete_session`
    commits.**

    This is the class that makes *"rows first, **committed**, then files"* a true statement about
    durability rather than about intent. `PurgeExpiredGuestSessions` deletes a session's row and
    only then unlinks its files, so that a crash between the two leaves a file with no row — an
    orphan the sweep reclaims — instead of a row pointing at bytes that are gone, which is the
    failure a user meets as a 410 on a download. That ordering is worth nothing if the `DELETE` is
    still sitting in an uncommitted transaction when the `unlink` happens, and the use case cannot
    say so: `ExpiredGuestDataPort.delete_session` names no transaction and `application/` may not
    name one either (ADR-0002). The boundary is therefore this file's, exactly as the "two commits"
    boundary for tailoring and export is.

    **One commit per session, not one per run**, and that is the same decision as the two sweeps'.
    A run is deliberately partial (R-3, R-4): a refused `DELETE` costs one `sessions_failed` and the
    batch continues, so every session already deleted must stay deleted when the run later fails
    part-way through. A single commit at the end would make a run all-or-nothing, and a batch that
    contains one permanently bad row would then purge nothing, for ever, while the backlog count
    climbed and the heartbeat kept saying `ok`.

    **Delegation rather than a subclass of `SqlAlchemyExpiredGuestData`**, for the import-order
    reason `CommittingTailoringRunRepository` documents at length: the adapter modules under
    `persistence/` read mapped attributes and build `Table` objects at *import* time, and a
    `class X(SqlAlchemyExpiredGuestData)` statement is a top-level import by another name. The four
    pass-throughs are the price; `mypy --strict` checking this against the `ExpiredGuestDataPort`
    Protocol is what keeps them from drifting.

    **No `expunge` before the inner `begin_nested()`**, unlike the two repositories above, and the
    difference is not an oversight. Those wrap a *repository*: a use case mutates an aggregate and
    then calls `save`, so the session is dirty on entry and `begin_nested()`'s unconditional entry
    flush would flush it outside the SAVEPOINT. This adapter loads no aggregate and adds nothing to
    the session — its `DELETE` is Core SQL — and the purge's composition roots hand it a session
    nothing else writes through, so there is never anything dirty to flush. The moment a root shares
    that session with a repository, the assumption breaks; that is a note on the root, not a
    defensive `expunge` here for an object this class cannot name.
    """

    def __init__(self, inner: ExpiredGuestDataPort, session: AsyncSession) -> None:
        self._inner = inner
        self._session = session

    async def count_expired(self, as_of: datetime) -> int:
        """A read, so **no commit** — the same rule as the repositories' read pass-throughs."""
        return await self._inner.count_expired(as_of)

    async def list_expired(self, as_of: datetime, limit: int) -> Sequence[ExpiringGuestSession]:
        """A read, so **no commit**. The batch is collected before anything is deleted (ADR-0006 §2)
        and the deletes that follow each commit on their own."""
        return await self._inner.list_expired(as_of, limit)

    async def delete_session(self, session_id: GuestSessionId) -> None:
        """The inner SAVEPOINT, **then commit** — the whole reason this class exists.

        The commit is what the file unlink that follows it in `PurgeExpiredGuestSessions` is allowed
        to rely on. It is also what bounds a failure: a run that dies after this returns leaves this
        session gone and every later candidate untouched, and the next tick lists only the rest.
        """
        await self._inner.delete_session(session_id)
        await self._session.commit()

    async def which_are_referenced(self, keys: Sequence[FileRef]) -> frozenset[FileRef]:
        """A read, so **no commit** — and the one read whose *failure* is load-bearing. It must
        propagate untouched, because `ReclaimOrphanedFiles` turns any exception here into
        `OrphanScanAborted` and unlinks nothing (R-33, AC-23)."""
        return await self._inner.which_are_referenced(keys)


class OverdueBacklog:
    """How many guest sessions are still expired-and-present, asked with the purge's own predicate.

    **This exists because `PurgeReport` deliberately does not carry `overdue_after`** and must not
    grow it: its field set is pinned by AC-4, and every field on it is a count of what *this run*
    did. "How much is still overdue" is a different question — it is a fact about the data, not
    about the run — and `ExpiredGuestDataPort.count_expired` is where the codebase already asks it
    (`/health/ready`'s `overdue` asks the identical question of the identical index). Widening the
    use case's return to carry it would give one value two producers, which is the 1.5 lesson about
    a second derivation, and it would put a number in the report that the report cannot vouch for:
    a concurrent purge or a fresh session can change it between the last delete and the count.

    So the entry point asks, after the run, and the backlog is what ADR-0018 decision 4 says to
    trust: a log line, a heartbeat and a run row can all be written by a job that is not working;
    a falling `overdue` cannot be faked by one. The line is a *snapshot taken after this run*, not a
    subtraction from `examined`.

    **The cutoff derivation lives here rather than in the task**, for the same one-derivation
    reason: `RetentionWindow.expiry_cutoff` is the rule for what "expired" means, and an entry point
    that re-derived it could drift from the listing it is reporting on.
    """

    def __init__(self, data: ExpiredGuestDataPort, clock: Clock, window: RetentionWindow) -> None:
        self._data = data
        self._clock = clock
        self._window = window

    async def count(self) -> int:
        """The backlog **now** — a fresh instant, not the run's.

        The purge's `now` is the instant its batch was selected at; by the time it finishes, minutes
        of sessions may have expired. Reporting the run's instant would understate a backlog on a
        box that is behind, which is exactly the box an operator is reading the line on.
        """
        return await self._data.count_expired(self._window.expiry_cutoff(self._clock.now()))
