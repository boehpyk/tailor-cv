"""The `TrackApplication` use case: put one of a signed-in user's succeeded runs on their board
(slice 3.1, technical plan §2, AC-8).

Flow: ``resolve_existing_user`` (`UserNotFound`) → ``runs(run_id, UserOwner(user_id))`` — tailoring's
`GetTailoringRun`, so the run's authorization and its 404 collapse are inherited rather than copied
(another user's, a guest's or a nonexistent run → `TailoringRunNotFound`) → ``status is SUCCEEDED``
else `TailoringRunNotTrackable(status.value)` → ``cards.find_for_run`` ⇒
`ApplicationAlreadyTracked(existing.id)` → ``cards.count_for_user >= cap`` ⇒
`TooManyTrackedApplications(cap)` → `TrackedApplication.track` → ``cards.add`` → publish. **No row on
any refusal.**

**Why it borrows `GetTailoringRun` rather than a new port:** authorizing a run is `tailoring`'s rule,
already written once; a `TrackableRunPort` would be a second copy of it. `EraseHistoryEntry` set the
precedent.
"""

from __future__ import annotations

from dataclasses import dataclass

from tailorcraft.application.identity.resolve_existing_user import resolve_existing_user
from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRun
from tailorcraft.domain.identity.ownership import UserOwner
from tailorcraft.domain.identity.ports import UserRepository
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.shared.clock import Clock
from tailorcraft.domain.shared.events import EventPublisherPort
from tailorcraft.domain.tailoring.value_objects import TailoringRunId, TailoringRunStatus
from tailorcraft.domain.tracking.errors import (
    ApplicationAlreadyTracked,
    TailoringRunNotTrackable,
    TooManyTrackedApplications,
)
from tailorcraft.domain.tracking.ports import TrackedApplicationRepository
from tailorcraft.domain.tracking.tracked_application import TrackedApplication
from tailorcraft.domain.tracking.value_objects import (
    ApplicationStage,
    ApplicationTitle,
    TrackedRunRef,
)


@dataclass(frozen=True, slots=True)
class TrackApplicationCommand:
    """A signed-in user asked to track one of their runs.

    `tailoring_run_id` is still **tailoring's** type here: it is handed to `GetTailoringRun`, and only
    the run that comes back is converted to tracking's `TrackedRunRef`. `title` arrives as an
    `ApplicationTitle`, already validated at the boundary (its `InvalidApplicationTitle` is a 422
    there), so this use case never re-checks it.
    """

    user_id: UserId
    tailoring_run_id: TailoringRunId
    stage: ApplicationStage = ApplicationStage.TO_APPLY
    title: ApplicationTitle | None = None


class TrackApplication:
    """Track `cmd.tailoring_run_id` for `cmd.user_id` and return the new card (module docstring for
    the flow and every refusal)."""

    def __init__(
        self,
        users: UserRepository,
        runs: GetTailoringRun,
        cards: TrackedApplicationRepository,
        events: EventPublisherPort,
        clock: Clock,
        cap: int = 500,
    ) -> None:
        self._users = users
        self._runs = runs
        self._cards = cards
        self._events = events
        self._clock = clock
        # `MAX_TRACKED_APPLICATIONS_PER_USER`; the in-code default matches the setting's (plan §0.9).
        self._cap = cap

    async def __call__(self, cmd: TrackApplicationCommand) -> TrackedApplication:
        await resolve_existing_user(self._users, cmd.user_id)
        # Tailoring's own rule authorizes the run: another user's, a guest's or a nonexistent run is
        # `TailoringRunNotFound` there, with its cause, and is never re-checked here.
        run = await self._runs(cmd.tailoring_run_id, UserOwner(cmd.user_id))
        if run.status is not TailoringRunStatus.SUCCEEDED:
            # A plain `str` crosses the seam: `domain/tracking` names no tailoring type.
            raise TailoringRunNotTrackable(run.status.value)

        # The one conversion between the two contexts (module docstring).
        run_ref = TrackedRunRef(run.id.value)
        # Before the cap: a repeat is answered with the card the user already has, even at the cap.
        existing = await self._cards.find_for_run(cmd.user_id, run_ref)
        if existing is not None:
            raise ApplicationAlreadyTracked(existing.id)
        if await self._cards.count_for_user(cmd.user_id) >= self._cap:
            raise TooManyTrackedApplications(self._cap)

        card = TrackedApplication.track(
            id=self._cards.next_identity(),
            user_id=cmd.user_id,
            tailoring_run_id=run_ref,
            stage=cmd.stage,
            title=cmd.title,
            at=self._clock.now(),
        )
        await self._cards.add(card)
        await self._events.publish(*card.release_events())
        return card


__all__ = ["TrackApplication", "TrackApplicationCommand"]
