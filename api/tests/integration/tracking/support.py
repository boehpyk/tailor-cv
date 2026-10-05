"""Shared arrangement for the tracking use-case tests (slice 3.1, T11).

Users, runs, postings and guest sessions are **real rows** in `tailorcraft_test` (the rolled-back
`session` fixture), reached through the real `SqlAlchemy…Repository` adapters — only the tracking
ports have in-memory doubles, because their adapters arrive at T14. Every seed **commits** (2.3's
lesson: on the `create_savepoint` session a commit releases a savepoint and the outer rollback still
isolates the test).
"""

from __future__ import annotations

import secrets
from datetime import datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.tailoring.get_tailoring_run import GetTailoringRun
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, UserId
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.domain.tracking.tracked_application import TrackedApplication
from tailorcraft.domain.tracking.value_objects import (
    ApplicationStage,
    ApplicationTitle,
    TrackedApplicationId,
    TrackedRunRef,
)
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.mapping.export.export_job import (  # noqa: F401
    export_job_table,
)
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (  # noqa: F401
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import (  # noqa: F401
    job_posting_table,
)
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (  # noqa: F401
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tests.integration.owners import (
    failed_run,
    pasted_posting,
    queued_run,
    running_run,
    succeeded_run,
)

_PASSWORD_HASH = PasswordHash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA")


async def seed_user(session: AsyncSession, clock: FixedClock, email: str) -> UserId:
    users = SqlAlchemyUserRepository(session)
    user_id = users.next_identity()
    await users.add(
        User.register_with_password(
            id=user_id,
            email=EmailAddress.parse(email),
            password_hash=_PASSWORD_HASH,
            at=clock.now(),
        )
    )
    await session.commit()
    return user_id


async def seed_guest(session: AsyncSession, clock: FixedClock) -> GuestOwner:
    sessions = SqlAlchemyGuestSessionRepository(session)
    guest = GuestSession.start(
        id=sessions.next_identity(),
        token_hash=secrets.token_hex(32),
        at=clock.now(),
        ttl_hours=24,
    )
    await sessions.add(guest)
    await session.commit()
    return GuestOwner(guest.id)


async def seed_run(session: AsyncSession, owner: Owner, at: datetime, kind: str) -> TailoringRun:
    """A run of `kind` (`queued` | `running` | `succeeded` | `failed`) for `owner`, over a real posting
    (the repository refuses a run whose posting is missing, unconditionally — 2.3)."""
    posting = pasted_posting(owner, at)
    await SqlAlchemyJobPostingRepository(session).add(posting)
    builders = {
        "queued": queued_run,
        "running": running_run,
        "succeeded": succeeded_run,
        "failed": failed_run,
    }
    run = builders[kind](owner, at, job_posting_id=posting.id)
    await SqlAlchemyTailoringRunRepository(session).add(run)
    await session.commit()
    return run


def user_owner(user_id: UserId) -> UserOwner:
    return UserOwner(user_id)


def a_card(
    user_id: UserId,
    at: datetime,
    *,
    stage: ApplicationStage = ApplicationStage.TO_APPLY,
    title: ApplicationTitle | None = None,
    run_id: TailoringRunId | None = None,
    card_id: TrackedApplicationId | None = None,
) -> TrackedApplication:
    """A card as a repository would hand it out: version 1, no pending event."""
    card = TrackedApplication.track(
        id=card_id if card_id is not None else TrackedApplicationId(value=uuid4()),
        user_id=user_id,
        tailoring_run_id=TrackedRunRef(run_id.value if run_id is not None else uuid4()),
        stage=stage,
        title=title,
        at=at,
    )
    card.release_events()
    return card


def real_users(session: AsyncSession) -> SqlAlchemyUserRepository:
    """The real `UserRepository`. Importing it here (after the mapping modules above) is what makes
    every tracking test's collection independent of import order — a mapped table must exist before
    a repository module reads its columns."""
    return SqlAlchemyUserRepository(session)


class SpyRuns(SqlAlchemyTailoringRunRepository):
    """The real run repository, recording every `get` — how "the user was resolved before any run
    was read" is observed without replacing the adapter."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.gets: list[TailoringRunId] = []

    async def get(self, run_id: TailoringRunId) -> TailoringRun:
        self.gets.append(run_id)
        return await super().get(run_id)


def real_get_tailoring_run(
    session: AsyncSession, runs: SqlAlchemyTailoringRunRepository, clock: FixedClock
) -> GetTailoringRun:
    return GetTailoringRun(
        runs, SqlAlchemyGuestSessionRepository(session), SqlAlchemyUserRepository(session), clock
    )
