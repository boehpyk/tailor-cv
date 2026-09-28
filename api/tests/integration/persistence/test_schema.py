"""Schema-level persistence tests that don't belong to either aggregate's repository module: the
AC-14 cascade delete and `ix_intake_base_cv_guest_session_id` (the sibling of
`test_guest_session_repository.py`'s `ix_identity_guest_session_expires_at` check).

Both indexes serve slice 1.6's purge before that slice exists (technical-plan.md's "Data &
migrations"): `ix_identity_guest_session_expires_at` for `WHERE expires_at < now()`, and this
module's `ix_intake_base_cv_guest_session_id` for the cascade delete itself, `GET /api/base-cvs`, and
`count_for_session`. Asserting both now is what stops either being quietly dropped by a future
migration edit that "cleans up" an index nothing in *this* slice's queries appears to use.

**T31 additions (AC-2, AC-12), test-after.** The purge (1.6) needed no migration at all — every
cascade, every index and the `NOT NULL` on `guest_session_id` across the four guest-owned tables
already existed, written one slice early with a comment naming this one. AC-2 is the test that
*proves* that claim by reading the live schema rather than trusting the comments that promised it
(`test_guest_session_id_is_not_null_indexed_and_cascades_to_identity_guest_session` below, run once
per table). AC-12 is a *different* claim about the same fact — not "the schema is complete" but "the
one column Phase 2.2 must widen is still exactly as narrow as the purge assumes" — so it gets its own
test and its own docstring even though the query underneath is identical today; the two will diverge
the day 2.2 makes the column nullable, and only one of them is supposed to.
"""

from __future__ import annotations

from typing import Final
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType, OriginalFilename
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingId, JobPostingText
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)

# The four tables AC-2 reads the schema of — the ones ADR-0006's retention obligation names as "a
# guest-owned row" (feature-spec.md's ubiquitous-language table). Index and cascade still hold for
# all four. Since 2.3 (ADR-0023) none of them is NOT NULL on `guest_session_id` any more: each can be
# user-owned, and `ck_<table>_exactly_one_owner` carries the invariant the NOT NULL used to.
_GUEST_OWNED_TABLES: Final[tuple[str, ...]] = (
    "intake_base_cv",
    "posting_job_posting",
    "tailoring_run",
    "export_job",
)


async def test_guest_session_id_index_exists_on_base_cv(session: AsyncSession) -> None:
    """Serves `GET /api/base-cvs`, `count_for_session`, and the cascade delete below."""
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'intake_base_cv' "
            "AND indexname = 'ix_intake_base_cv_guest_session_id'"
        )
    )
    assert result.scalar_one_or_none() == "ix_intake_base_cv_guest_session_id"


async def test_deleting_a_guest_session_cascades_to_its_base_cvs(
    session: AsyncSession, clock: FixedClock
) -> None:
    """AC-14: `intake_base_cv.guest_session_id` is a `NOT NULL` FK with `ON DELETE CASCADE`, so
    deleting a session row removes every `BaseCv` it owns in one statement — no join the 1.6 purge
    job could get wrong.

    The delete is issued as a raw statement against `guest_session_table`, not through
    `GuestSession`/`SqlAlchemyGuestSessionRepository` (this slice ships no delete path — the purge
    job is 1.6): the point of this test is what the *database* does when a session row disappears,
    independent of which future caller triggers it.
    """
    sessions = SqlAlchemyGuestSessionRepository(session)
    owner = GuestSession.start(
        id=sessions.next_identity(), token_hash="f" * 64, at=clock.now(), ttl_hours=24
    )
    await sessions.add(owner)

    cvs = SqlAlchemyBaseCvRepository(session)
    cv_id = cvs.next_identity()
    cv = BaseCv.upload(
        id=cv_id,
        owner=GuestOwner(owner.id),
        original_filename=OriginalFilename("cv.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=1,
        file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
        uploaded_at=clock.now(),
    )
    await cvs.add(cv)

    # Columns are `TypeDecorator`-backed (`GuestSessionIdType` / `BaseCvIdType`), so the bound
    # parameter has to be the domain value object the decorator's `process_bind_param` expects
    # (`owner.id`, `cv_id`) — not the bare `UUID` underneath it.
    await session.execute(guest_session_table.delete().where(guest_session_table.c.id == owner.id))
    await session.flush()

    remaining = await session.execute(select(base_cv_table.c.id).where(base_cv_table.c.id == cv_id))
    assert remaining.scalar_one_or_none() is None


# --- T31 / AC-2: no migration needed — proven by reading the live schema --------------------------


@pytest.mark.parametrize("table_name", _GUEST_OWNED_TABLES)
async def test_guest_session_id_is_not_null_indexed_and_cascades_to_identity_guest_session(
    session: AsyncSession, table_name: str
) -> None:
    """AC-2. For each of the four guest-owned tables: `guest_session_id` is the first column of at
    least one index (the cascade delete and, on `intake_base_cv`/`export_job`, a real application
    query both depend on it existing) and carries a foreign key to `identity_guest_session.id` with
    `ON DELETE CASCADE` (`confdeltype = 'c'`). Since 2.2 (`intake_base_cv`) and 2.3 (the other
    three, ADR-0023) the column is nullable on all four — the row can be user-owned — and
    `ck_<table>_exactly_one_owner` carries the "exactly one owner" invariant the `NOT NULL` used to.

    This is a proof, not a discovery: the task list and CLAUDE.md both record that every column here
    was written correctly one slice early, specifically so 1.6 would add no migration. The test
    exists so a future edit that "cleans up" an index nothing in *this* slice's queries appears to
    use goes red — reading `pg_constraint`/`pg_index` rather than `test_schema.py`'s own prose is
    what makes that a fact about the database instead of a fact about a comment.
    """
    not_null = await session.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = :table_name AND column_name = 'guest_session_id'"
        ),
        {"table_name": table_name},
    )
    assert not_null.scalar_one() == "YES", (
        f"{table_name}.guest_session_id is NOT NULL — 2.2 (intake_base_cv) and 2.3 (the other three)"
        " made it nullable so the row can be user-owned"
    )
    constraint_name = f"ck_{table_name}_exactly_one_owner"
    check = await session.execute(
        text(
            "SELECT pg_get_constraintdef(con.oid) FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "WHERE rel.relname = :table_name AND con.conname = :constraint_name"
        ),
        {"table_name": table_name, "constraint_name": constraint_name},
    )
    definition = check.scalar_one_or_none()
    assert definition is not None, f"{table_name} carries no constraint named {constraint_name}"
    assert "num_nonnulls(guest_session_id, user_id) = 1" in definition, (
        f"{constraint_name}'s definition is {definition!r}, not the 'exactly one owner' invariant"
    )

    indexed = await session.execute(
        text(
            "SELECT count(*) FROM pg_index idx "
            "JOIN pg_class rel ON rel.oid = idx.indrelid "
            "JOIN pg_attribute att ON att.attrelid = rel.oid AND att.attnum = ANY(idx.indkey) "
            "WHERE rel.relname = :table_name AND att.attname = 'guest_session_id'"
        ),
        {"table_name": table_name},
    )
    assert indexed.scalar_one() > 0, f"{table_name}.guest_session_id carries no index"

    cascade = await session.execute(
        text(
            "SELECT con.confdeltype::text, refrel.relname FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_class refrel ON refrel.oid = con.confrelid "
            "JOIN pg_attribute att ON att.attrelid = rel.oid AND att.attnum = ANY(con.conkey) "
            "WHERE con.contype = 'f' AND rel.relname = :table_name AND att.attname = 'guest_session_id'"
        ),
        {"table_name": table_name},
    )
    delete_type, referenced_table = cascade.one()
    assert referenced_table == "identity_guest_session", (
        f"{table_name}.guest_session_id's FK does not reference identity_guest_session"
    )
    assert delete_type == "c", (
        f"{table_name}.guest_session_id's FK is not ON DELETE CASCADE (confdeltype={delete_type!r})"
    )


async def test_identity_guest_session_expires_at_is_indexed(session: AsyncSession) -> None:
    """AC-2's other half: the purge's own predicate, `expires_at <= now()`, is served by an index
    rather than a sequential scan of every guest session there has ever been."""
    result = await session.execute(
        text(
            "SELECT count(*) FROM pg_index idx "
            "JOIN pg_class rel ON rel.oid = idx.indrelid "
            "JOIN pg_attribute att ON att.attrelid = rel.oid AND att.attnum = ANY(idx.indkey) "
            "WHERE rel.relname = 'identity_guest_session' AND att.attname = 'expires_at'"
        )
    )
    assert result.scalar_one() > 0


# --- T32: one DELETE cascades across all four guest-owned tables in a single statement -------------


async def test_deleting_a_guest_session_cascades_to_all_four_guest_owned_tables(
    session: AsyncSession, clock: FixedClock
) -> None:
    """T32. `test_deleting_a_guest_session_cascades_to_its_base_cvs` above proves the mechanism for
    one table; this is the same proof for all four at once, in response to the *same* single
    statement the purge's `delete_session` issues — matching AC-11's "across all four tables"
    wording. `tailoring_run.base_cv_id` / `job_posting_id` and `export_job.tailoring_run_id` carry no
    foreign key of their own (by design — see the mapping modules), so this test builds four
    genuinely independent rows rather than a chain, exactly as the purge's own adapter sees them.
    """
    sessions = SqlAlchemyGuestSessionRepository(session)
    owner = GuestSession.start(
        id=sessions.next_identity(), token_hash="e" * 64, at=clock.now(), ttl_hours=24
    )
    await sessions.add(owner)

    cvs = SqlAlchemyBaseCvRepository(session)
    cv_id = cvs.next_identity()
    await cvs.add(
        BaseCv.upload(
            id=cv_id,
            owner=GuestOwner(owner.id),
            original_filename=OriginalFilename("cv.pdf"),
            content_type=CvContentType.PDF,
            size_bytes=1,
            file=FileRef.for_base_cv(cv_id, CvContentType.PDF),
            uploaded_at=clock.now(),
        )
    )

    postings = SqlAlchemyJobPostingRepository(session)
    posting_id = postings.next_identity()
    await postings.add(
        JobPosting.from_pasted_text(
            id=posting_id,
            owner=GuestOwner(owner.id),
            text=JobPostingText("x" * 150),
            created_at=clock.now(),
        )
    )

    runs = SqlAlchemyTailoringRunRepository(session)
    run_id = runs.next_identity()
    await runs.add(
        TailoringRun.request(
            id=run_id,
            owner=GuestOwner(owner.id),
            base_cv_id=BaseCvId(value=uuid4()),
            job_posting_id=JobPostingId(value=uuid4()),
            requested_at=clock.now(),
        )
    )

    jobs = SqlAlchemyExportJobRepository(session)
    job_id = jobs.next_identity()
    await jobs.add(
        ExportJob.request(
            id=job_id,
            owner=GuestOwner(owner.id),
            tailoring_run_id=TailoringRunId(value=uuid4()),
            document=TailoredDocumentKind.CV,
            format=ExportFormat.PDF,
            run_version=1,
            requested_at=clock.now(),
        )
    )

    await session.execute(guest_session_table.delete().where(guest_session_table.c.id == owner.id))
    await session.flush()

    for table, row_id in (
        (base_cv_table, cv_id),
        (job_posting_table, posting_id),
        (tailoring_run_table, run_id),
        (export_job_table, job_id),
    ):
        remaining = await session.execute(select(table.c.id).where(table.c.id == row_id))
        assert remaining.scalar_one_or_none() is None, f"a row survived in {table.name}"


# --- T15 / AC-13: the user half of the owner — CHECK, FK and index by name, read from the catalog --


async def test_ck_intake_base_cv_label_only_when_user_owned_by_name_and_definition(
    session: AsyncSession,
) -> None:
    """AC-13's second CHECK: I-7's "a label only on a saved CV", read from `pg_constraint` by the
    exact name the migration gave it, never trusted from the mapping module's comment."""
    result = await session.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'intake_base_cv'::regclass "
            "AND conname = 'ck_intake_base_cv_label_only_when_user_owned'"
        )
    )
    definition = result.scalar_one_or_none()
    assert definition is not None, (
        "intake_base_cv carries no constraint named ck_intake_base_cv_label_only_when_user_owned"
    )
    assert "label IS NULL" in definition, (
        f"ck_intake_base_cv_label_only_when_user_owned's definition is {definition!r}, missing "
        "the 'label IS NULL' branch of I-7's rule"
    )
    assert "user_id IS NOT NULL" in definition, (
        f"ck_intake_base_cv_label_only_when_user_owned's definition is {definition!r}, missing "
        "the 'user_id IS NOT NULL' branch of I-7's rule"
    )


async def test_fk_intake_base_cv_user_id_identity_user_is_named_and_cascades_on_delete(
    session: AsyncSession,
) -> None:
    """AC-13: `user_id`'s foreign key, by the name `SqlAlchemyBaseCvRepository.add` recognises via
    `violated_constraint` (S-12) and `SqlAlchemyAccountData.delete_account`'s cascade (AC-11) both
    depend on — proven here as a fact about the schema, independent of either adapter."""
    result = await session.execute(
        text(
            "SELECT con.confdeltype::text, refrel.relname FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_class refrel ON refrel.oid = con.confrelid "
            "WHERE con.contype = 'f' AND con.conname = 'fk_intake_base_cv_user_id_identity_user' "
            "AND rel.relname = 'intake_base_cv'"
        )
    )
    row = result.one_or_none()
    assert row is not None, (
        "intake_base_cv carries no foreign key named fk_intake_base_cv_user_id_identity_user"
    )
    delete_type, referenced_table = row
    assert referenced_table == "identity_user"
    assert delete_type == "c", (
        f"fk_intake_base_cv_user_id_identity_user is not ON DELETE CASCADE ({delete_type!r})"
    )


async def test_ix_intake_base_cv_user_id_exists(session: AsyncSession) -> None:
    """Serves `list_for_user`, `count_for_user`, `SqlAlchemyAccountData.files_of_account` and the
    cascade above."""
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'intake_base_cv' AND indexname = 'ix_intake_base_cv_user_id'"
        )
    )
    assert result.scalar_one_or_none() == "ix_intake_base_cv_user_id"
