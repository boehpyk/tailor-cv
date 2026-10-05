"""Shared arrangement for slice 3.1's HTTP tests (T21 RED): the five tracking routes under
`/api/me/`.

**Rows are seeded through the real repositories** on the test's own `session` (the one the shared
`app` fixture serves requests from), never through the routes under test — except in the tests that
are *about* the create route. That is what makes a skeleton's failure land on the assertion about the
route (`assert 500 == 200`) and never on a seed that needed the route to work.

A card seed is a **plain-value snapshot** (`Seeded`): every request that ends in a rollback expires
the instances the shared session holds, and a later `card.id` would be a lazy load —
`MissingGreenlet` on an `AsyncSession` (2.3's `Entry` note).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from httpx import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tailorcraft.domain.tracking.value_objects import ApplicationStage, ApplicationTitle
from tailorcraft.infrastructure.persistence.repositories.tracking.tracked_application import (
    SqlAlchemyTrackedApplicationRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.api.me_support import Account, Entry, seed_entry
from tests.integration.tracking.support import a_card

ME_BOARD = "/api/me/board"
ME_TRACKED = "/api/me/tracked-applications"

STAGES = ("to_apply", "applied", "interviewing", "offer", "rejected", "withdrawn")

TRACKED_APPLICATION_KEYS = {
    "id",
    "tailoring_run_id",
    "stage",
    "title",
    "tracked_at",
    "stage_changed_at",
    "version",
}
BOARD_CARD_KEYS = TRACKED_APPLICATION_KEYS | {"run", "posting", "base_cv"}
BOARD_RUN_KEYS = {"requested_at", "edited"}
BOARD_POSTING_KEYS = {"job_posting_id", "source", "title", "source_url", "preview"}
BOARD_BASE_CV_KEYS = {"base_cv_id", "label", "original_filename"}


@dataclass(frozen=True)
class Seeded:
    """One seeded card over one seeded history entry, as plain values."""

    card_id: UUID
    run_id: UUID
    entry: Entry
    stage: str
    version: int

    @property
    def url(self) -> str:
        return f"{ME_TRACKED}/{self.card_id}"

    @property
    def stage_url(self) -> str:
        return f"{self.url}/stage"

    @property
    def title_url(self) -> str:
        return f"{self.url}/title"


async def seed_card(
    session: AsyncSession,
    settings: Settings,
    account: Account,
    *,
    at: datetime,
    stage: ApplicationStage = ApplicationStage.TO_APPLY,
    title: str | None = None,
    entry: Entry | None = None,
) -> Seeded:
    """A succeeded history entry for `account` (unless `entry` is given) and a card on it, both
    committed. No export files: the cards' tests that care about files seed their own entry."""
    the_entry = entry or await seed_entry(session, settings, account.owner, at=at, ready_formats=())
    repo = SqlAlchemyTrackedApplicationRepository(session)
    card = a_card(
        account.user_id,
        at,
        stage=stage,
        title=ApplicationTitle(title) if title is not None else None,
        run_id=the_entry.run_id,
        card_id=repo.next_identity(),
    )
    snapshot = Seeded(
        card_id=card.id.value,
        run_id=the_entry.run_id.value,
        entry=the_entry,
        stage=stage.value,
        version=card.version,
    )
    await repo.add(card)
    await session.commit()
    return snapshot


async def seed_orphan_card(session: AsyncSession, account: Account, *, at: datetime) -> UUID:
    """A card whose run row does not exist — T-36. Only raw construction builds one (AC-18's locks
    prevent it), and the repository's `add` refuses it, so the row goes in as SQL."""
    card_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO tracking_application (id, user_id, tailoring_run_id, stage, title, "
            "tracked_at, stage_changed_at, version) "
            "VALUES (:id, :user_id, :run_id, 'to_apply', NULL, :at, :at, 1)"
        ),
        {"id": card_id, "user_id": account.user_id.value, "run_id": uuid4(), "at": at},
    )
    await session.commit()
    return card_id


async def card_row(session: AsyncSession, card_id: UUID) -> dict[str, object] | None:
    """Every column of one card, as stored — read through `text()` so no identity map answers."""
    session.expire_all()  # a Core UPDATE does not touch the identity map (2.4)
    result = await session.execute(
        text("SELECT * FROM tracking_application WHERE id = :id"), {"id": card_id}
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def card_count(session: AsyncSession, user_id: UUID) -> int:
    result = await session.execute(
        text("SELECT count(*) FROM tracking_application WHERE user_id = :u"), {"u": user_id}
    )
    return int(result.scalar_one())


def assert_no_store(response: Response) -> None:
    assert response.headers.get("cache-control") == "no-store", dict(response.headers)


async def assert_no_card_crosses_owners(session: AsyncSession, user_ids: list[UUID]) -> int:
    """AC-29: for the given users, no card whose user differs from its run's user, and no card whose
    run does not exist. Returns how many cards it looked at, so a caller can pin a **positive
    control** (a count of zero would make both zero-row assertions vacuous)."""
    session.expire_all()
    checked = int(
        (
            await session.execute(
                text("SELECT count(*) FROM tracking_application WHERE user_id = ANY(:u)"),
                {"u": user_ids},
            )
        ).scalar_one()
    )
    crossing = int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM tracking_application a "
                    "JOIN tailoring_run r ON r.id = a.tailoring_run_id "
                    "WHERE a.user_id = ANY(:u) AND r.user_id IS DISTINCT FROM a.user_id"
                ),
                {"u": user_ids},
            )
        ).scalar_one()
    )
    runless = int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM tracking_application a "
                    "WHERE a.user_id = ANY(:u) AND NOT EXISTS "
                    "(SELECT 1 FROM tailoring_run r WHERE r.id = a.tailoring_run_id)"
                ),
                {"u": user_ids},
            )
        ).scalar_one()
    )
    assert crossing == 0, f"{crossing} card(s) point at a run another user owns"
    assert runless == 0, f"{runless} card(s) point at a run that does not exist"
    return checked


async def card_count_on(engine: AsyncEngine, user_id: UUID) -> int:
    """A user's cards, read on a **fresh connection** — what is committed, not what a session holds."""
    async with engine.connect() as probe:
        return int(
            (
                await probe.execute(
                    text("SELECT count(*) FROM tracking_application WHERE user_id = :u"),
                    {"u": user_id},
                )
            ).scalar_one()
        )
