"""Seed a *working copy* — a guest CV whose `copied_from_base_cv_id` is set — without the copy.

2.2's `BaseCv.copy_from` made these rows and 2.4 retires it (AC-33, OQ-2), but rows it already made
still exist on the box until the purge takes them, and the claim, the purge and the history read
`copied_from_base_cv_id`. Tests of those readers need the *row*, not the transition, so they seed it
here: an ordinary upload, then the provenance column set in SQL (it carries no FK, ADR-0022).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from tailorcraft.domain.identity.ownership import Owner
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tests.integration.owners import extracted_cv


async def seed_working_copy(
    session: AsyncSession, owner: Owner, at: datetime, *, copied_from: BaseCvId
) -> BaseCv:
    """An `EXTRACTED` CV owned by `owner`, added and flushed (not committed), whose provenance is
    `copied_from`. The loaded aggregate reads `copied_from` too, so a caller need not reload."""
    cv = extracted_cv(owner, at)
    await SqlAlchemyBaseCvRepository(session).add(cv)
    await session.flush()
    await session.execute(
        text("UPDATE intake_base_cv SET copied_from_base_cv_id = :src WHERE id = :id"),
        {"src": copied_from.value, "id": cv.id.value},
    )
    set_committed_value(cv, "_copied_from", copied_from)
    return cv
