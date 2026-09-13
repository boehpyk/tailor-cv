"""The `ReviseTailoredDocument` use case: the editor's write path — replace the CV or the cover
letter on a `succeeded` run with the visitor's revision, versioned by the aggregate (ADR-0015).

One use case, two commands, one `__call__` with a `match` — the `CaptureJobPosting` shape from 1.2,
and chosen here for a stronger reason than there. In 1.2 the two arms differed in *where the text
came from*; here they differ in **what type the text is**. `ReviseCvCommand.content` is a
`TailoredCv` and `ReviseCoverLetterCommand.content` is a `CoverLetter`, and each arm calls the
aggregate method whose signature accepts exactly that type. A single command carrying `kind` plus a
`str` would have to construct the value object *inside* the use case — moving the length rules and
their `DomainError` → 422 translation away from the boundary where every other value object in this
codebase is built — and its `match` would then re-derive from `kind` what the command's type already
says. Two commands let the type system refuse a letter where a CV was meant before a line of this
module runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRunForSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.tailoring.ports import TailoringRunRepository
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import CoverLetter, TailoredCv, TailoringRunId


@dataclass(frozen=True, slots=True)
class ReviseCvCommand:
    """The visitor saved an edit to the tailored CV.

    `content` arrives as a `TailoredCv`, already validated: the boundary builds the value object
    and translates its `DomainError` into a 422, so the revision is held to the same bounds as the
    model's own draft (ADR-0015 §2) and this use case never re-checks them. `expected_version` is
    the run version the client was shown when it began editing — the aggregate compares it against
    its own (TR-9) and refuses a stale edit with `TailoredDocumentVersionConflict`.
    """

    tailoring_run_id: TailoringRunId
    guest_session_id: GuestSessionId
    content: TailoredCv
    expected_version: int


@dataclass(frozen=True, slots=True)
class ReviseCoverLetterCommand:
    """The visitor saved an edit to the cover letter.

    Same contract as `ReviseCvCommand` with `content` a `CoverLetter`. The two commands are
    deliberately not one command with a `kind` field — see the module docstring.
    """

    tailoring_run_id: TailoringRunId
    guest_session_id: GuestSessionId
    content: CoverLetter
    expected_version: int


ReviseTailoredDocumentCommand = ReviseCvCommand | ReviseCoverLetterCommand
"""The tagged union the use case matches over. The wire-format counterpart is the `PUT` route per
document in `infrastructure/api/routers/tailoring.py` — the URL names the kind, the body carries
the text and the version, and the router builds whichever command the URL selects."""


class ReviseTailoredDocument:
    """Replace one document on a visitor's `succeeded` run with their revision, and return the run
    at its new version.

    **It takes the read *use case*, `GetTailoringRunForSession`, not the repository — and that is
    the load-bearing choice.** The read use case carries the authorization rule: *what authorizes
    access is the link*, `run.guest_session_id == the resolved session id`, checked on every read
    (ADR-0008, ADR-0010). It also carries the collapse that goes with it — "not mine" raises
    `TailoringRunNotFound`, the same type as "does not exist", so the API answers 404 and **never
    403** (G-29/AC-14): a run id is the polling handle, so it is the id someone is most likely to be
    enumerating, and a 403 would confirm that a guessed id is real. This is the editor's write path
    and therefore a **new entry point** to a `TailoringRun`; composing the read use case is what
    1.3's `RequestTailoringRun` did with the CV and posting reads for exactly this reason. A use
    case that never sees a repository cannot forget the rule, and cannot re-implement it
    differently. The cost is the read it needed anyway.

    Flow (technical-plan.md, "Application layer"):

    1. ``run = await get_run(cmd.tailoring_run_id, cmd.guest_session_id)`` — resolves the session
       (`GuestSessionExpired`, E-5), loads, and raises `TailoringRunNotFound` for absent **and** not
       mine (E-6, with `TailoringRunNotOwnedBySession` on `__cause__`).
    2. ``match cmd:`` → ``run.revise_cv(cmd.content, expected_version=…, at=clock.now())`` or
       ``run.revise_cover_letter(...)``. `TailoringRunNotEditable` (E-7) and
       `TailoredDocumentVersionConflict` (E-8) propagate — nothing to record; the run is unchanged.
    3. ``await runs.save(run)`` — `TailoringRunConcurrentlyModified` (E-9) **propagates**; see
       below for why this use case does not catch it while `ExecuteTailoringRun` does.
    4. ``await events.publish(*run.release_events())`` — after the save, never before.
    5. Return the `TailoringRun`. The router serializes it exactly as the `GET` does, so the client
       receives the same shape it polls and can write it straight into its cache.

    **Nothing is written on any rejection.** Steps 1 and 2 raise before step 3, so E-5 through E-8
    leave no `save` and publish nothing.

    **Why `TailoringRunConcurrentlyModified` propagates here and is caught in the worker.** The
    same error, raised by the same repository translation, gets two different answers because the
    two callers are in different positions to act on it. `ExecuteTailoringRun` runs inside a Celery
    task with an **outcome vocabulary** (`ExecuteTailoringRunOutcome`) and a task that must not
    raise for a business outcome — a raise there is a failed task, retried or logged as a crash,
    for something that is not a crash but "another delivery started this run first". So the worker
    catches it and returns `SKIPPED` before paying. This use case runs inside an HTTP request with a
    **status code** to answer: 409 `document_version_conflict`, `current_version: null` (the
    aggregate this use case holds is the stale one; the true number lives in a row it has just been
    told it does not have). The caller is a person with a tab open who must act — reload and
    compare (E-15b) — and the only honest thing the use case can do is tell them, which is what an
    exception reaching the router's error boundary does. Catching it here would mean inventing a
    result type to say "did not happen", for a caller that has nothing to do with it but raise
    again.

    **Idempotency, stated so nobody adds a retry.** A retried `PUT` with the same
    `expected_version` after a lost response is *refused* (E-8), not applied twice. That is the
    correct idempotency for an edit: the server holds exactly one copy of what the user meant, and a
    second apply of the same text would still bump the version and mislead a third tab. Recovery is
    comparison on the client, not a retry with a fresh version.
    """

    def __init__(
        self,
        runs: TailoringRunRepository,
        get_run: GetTailoringRunForSession,
        events: EventPublisherPort,
        clock: Clock,
    ) -> None:
        self._runs = runs
        self._get_run = get_run
        self._events = events
        self._clock = clock

    async def __call__(self, cmd: ReviseTailoredDocumentCommand) -> TailoringRun:
        # Step 1. The composed read carries the authorization rule and the 404 collapse, so
        # `GuestSessionExpired` (E-5) and `TailoringRunNotFound` (E-6, absent *and* not mine)
        # propagate from here without this use case ever seeing a session or a foreign run.
        run = await self._get_run(cmd.tailoring_run_id, cmd.guest_session_id)

        # Step 2. The arms differ in the *type* of `cmd.content` (see the module docstring), and
        # each calls the aggregate method whose signature accepts exactly that type.
        # `TailoringRunNotEditable` (E-7) and `TailoredDocumentVersionConflict` (E-8) propagate:
        # the aggregate refuses before it mutates, so there is nothing to record and nothing to
        # undo — the run is exactly as it was loaded.
        match cmd:
            case ReviseCvCommand():
                run.revise_cv(
                    cmd.content, expected_version=cmd.expected_version, at=self._clock.now()
                )
            case ReviseCoverLetterCommand():
                run.revise_cover_letter(
                    cmd.content, expected_version=cmd.expected_version, at=self._clock.now()
                )
            case _:  # pragma: no cover — unreachable while the union has exactly two members
                # As in `CaptureJobPosting`: mypy narrows `cmd` to `Never` here only if the cases
                # above are exhaustive, so a third command added to the union without an arm is a
                # type error naming it rather than a silently unrevised run.
                assert_never(cmd)

        # Step 3. `TailoringRunConcurrentlyModified` (E-9) **propagates** — deliberately the
        # opposite of `ExecuteTailoringRun` step 4, which catches the same error from the same
        # repository translation and returns `SKIPPED`. The worker has an outcome vocabulary and a
        # task that must not raise for a business outcome; this use case has an HTTP request with
        # a status code to answer (409), and the caller is a person with a tab open who must act
        # (reload and compare, E-15b). Catching it here would invent a result type to say "did not
        # happen" for a router that has nothing to do with it but raise again. The aggregate this
        # use case holds is now the stale copy, and nothing below runs: the row is untouched and
        # the recorded `TailoredDocumentRevised` stays in the buffer, never published.
        await self._runs.save(run)

        # Step 4. After the save, never before — a publish that ran first would announce a
        # revision a failed save is about to un-happen.
        await self._events.publish(*run.release_events())

        # Step 5. The run at its new version; the router serializes it exactly as the `GET` does.
        return run


__all__ = [
    "ReviseCoverLetterCommand",
    "ReviseCvCommand",
    "ReviseTailoredDocument",
    "ReviseTailoredDocumentCommand",
]
