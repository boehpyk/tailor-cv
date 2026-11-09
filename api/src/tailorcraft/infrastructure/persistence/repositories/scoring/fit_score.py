"""`SqlAlchemyFitScoreRepository`: the `FitScoreRepository` port (ADR-0007, ADR-0040, slice 5.1).

**`store` is Core, and `score` never joins the session.** The plan's shape is `INSERT … ON CONFLICT DO
NOTHING`, which the ORM's flush cannot say. So a refused store has no pending instance to expire, and
the `expunge` before `begin_nested()` that `SqlAlchemyTailoringRunRepository`'s callers need has nothing
to act on here. `from_assessment` hands the use case a transient aggregate, and `store` returns that same
object when it won.

**The statements, in one SAVEPOINT** (plan §0.3), so a refusal leaves the surrounding transaction able
to commit the settlement that follows:

1. `INSERT … ON CONFLICT DO NOTHING RETURNING id`. Under READ COMMITTED a loser of the two-tab race
   waits for the winner's commit and then inserts nothing; if the winner rolled back, it inserts. The
   FK checks fire here on a real insert, taking the owner row `FOR KEY SHARE`, which is what meets
   erasure and the purge before any CV lock. A conflict inserts nothing, so **a loser takes no owner
   lock** and goes straight to step 2.
2. `FOR KEY SHARE` on the CV, then the posting. These tables have no FK from here (ADR-0014), so the
   locks close the race one would: whichever of store and deletion goes first, no score outlives its
   CV, because the deleter's score `DELETE` is a separate statement with a fresh snapshot. Either row
   gone rolls the savepoint back and raises.
3. A loser re-reads the winner, **after the locks**: if a deletion took the winner's CV and the winner
   between steps 1 and 2, the locks answer the 404 that a read coming first would have turned into
   "the winner vanished".

Since slice 5.2 a draft's score (`draft_of` set) also takes `FOR KEY SHARE` on its `tailoring_run` row,
after the CV and posting locks, for the same reason: no FK, and a history-entry deletion's draft-score
`DELETE` runs after its run `DELETE`, so whichever goes first, no draft score outlives its run.

`read=True` is load-bearing: `with_for_update(key_share=True)` alone renders `FOR NO KEY UPDATE`
(CLAUDE.md, and the 2.3 defect left in `tailoring_run.py`).

Filters use the `Table`'s columns, never `FitScore`'s read-only properties, which would build no
predicate (`repositories/tailoring/tailoring_run.py`'s module note).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Final, assert_never

from sqlalchemy import ColumnElement, Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import ReturningInsert

from tailorcraft.domain.identity.errors import GuestSessionExpired, UserNotFound
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.errors import JobPostingNotFound
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.scoring.fit_score import FitScore
from tailorcraft.domain.scoring.value_objects import (
    CvFingerprint,
    FitScoreId,
    PostingFingerprint,
    ScoredCvRef,
    ScoredPostingRef,
    ScoredRunRef,
)
from tailorcraft.domain.tailoring.errors import TailoringRunNotFound
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.scoring.fit_score import fit_score_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)

if TYPE_CHECKING:
    from tailorcraft.domain.scoring.ports import FitScoreRepository

_table = fit_score_table

# Recognised by name, never by message (`violated_constraint`). Renaming either in the mapping or the
# migration is a breaking change to `store`.
_USER_FK: Final = "fk_scoring_fit_score_user_id_identity_user"
_GUEST_FK: Final = "fk_scoring_fit_score_guest_session_id_identity_guest_session"


def _insert_if_absent(score: FitScore) -> ReturningInsert[tuple[FitScoreId]]:
    """The score's private attributes, which is what the mapping persists. Any unique constraint
    refusing is the two-tab rule, so no conflict target is named."""
    return (
        insert(_table)
        .values(
            id=score._id,
            guest_session_id=score._owner_guest_session_id,
            user_id=score._owner_user_id,
            base_cv_id=score._base_cv_id,
            job_posting_id=score._job_posting_id,
            cv_fingerprint=score._cv_fingerprint,
            posting_fingerprint=score._posting_fingerprint,
            score=score._score,
            gaps=score._gaps,
            model=score._model,
            prompt_version=score._prompt_version,
            input_tokens=score._input_tokens,
            output_tokens=score._output_tokens,
            unmetered_attempts=score._unmetered_attempts,
            llm_duration_ms=score._llm_duration_ms,
            cost_microusd=score._cost,
            scored_at=score._scored_at,
            tailoring_run_id=score._draft_of,
        )
        .on_conflict_do_nothing()
        .returning(_table.c.id)
    )


def _key_share_base_cv(ref: ScoredCvRef) -> Select[tuple[BaseCvId]]:
    """The seam between the contexts' id types, in the one adapter allowed to know both."""
    return (
        select(base_cv_table.c.id)
        .where(base_cv_table.c.id == BaseCvId(ref.value))
        .with_for_update(read=True, key_share=True)
    )


def _key_share_job_posting(ref: ScoredPostingRef) -> Select[tuple[JobPostingId]]:
    return (
        select(job_posting_table.c.id)
        .where(job_posting_table.c.id == JobPostingId(ref.value))
        .with_for_update(read=True, key_share=True)
    )


def _key_share_tailoring_run(ref: ScoredRunRef) -> Select[tuple[TailoringRunId]]:
    return (
        select(tailoring_run_table.c.id)
        .where(tailoring_run_table.c.id == TailoringRunId(ref.value))
        .with_for_update(read=True, key_share=True)
    )


def _owned_by(owner: Owner) -> ColumnElement[bool]:
    match owner:
        case GuestOwner(guest_session_id=guest_session_id):
            return _table.c.guest_session_id == guest_session_id
        case UserOwner(user_id=user_id):
            return _table.c.user_id == user_id
        case _:
            assert_never(owner)


class SqlAlchemyFitScoreRepository:
    """Persistence for `FitScore`. The unit of work is the caller's: nothing here commits, and the
    settlement's commit makes a stored score durable (plan §0.2)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def next_identity(self) -> FitScoreId:
        """Synchronous: application-assigned UUIDv7 needs no I/O (ADR-0007)."""
        return FitScoreId(uuid7())

    async def store(self, score: FitScore) -> FitScore:
        """The module docstring's statements. Returns `score`, or the winner's row."""
        try:
            async with self._session.begin_nested():
                inserted = (
                    await self._session.execute(_insert_if_absent(score))
                ).scalar_one_or_none()
                # Raised inside the SAVEPOINT so its rollback discards the row just inserted. Ids only
                # in the message (Constitution §8).
                cv = await self._session.execute(_key_share_base_cv(score.base_cv_id))
                if cv.scalar_one_or_none() is None:
                    raise BaseCvNotFound(f"no BaseCv with id {score.base_cv_id.value}")
                posting = await self._session.execute(_key_share_job_posting(score.job_posting_id))
                if posting.scalar_one_or_none() is None:
                    raise JobPostingNotFound(f"no JobPosting with id {score.job_posting_id.value}")
                if score.draft_of is not None:
                    run = await self._session.execute(_key_share_tailoring_run(score.draft_of))
                    if run.scalar_one_or_none() is None:
                        raise TailoringRunNotFound(
                            f"no TailoringRun with id {score.draft_of.value}"
                        )
                if inserted is not None:
                    return score
                winner = await self.find_for_pair(
                    score.owner, score.cv_fingerprint, score.posting_fingerprint
                )
                if winner is None:
                    # The winner hung off a re-upload of the same text, on another CV, and that CV
                    # was deleted in this instant. Nothing honest to return: not a `DomainError`, so
                    # the transaction rolls back and the reservation stands (S-15's answer).
                    raise RuntimeError(f"the fit score that beat {score.id.value} has gone")
                return winner
        except IntegrityError as exc:
            constraint = violated_constraint(exc)
            # `from None`: the listener already reduced the chain to identifiers, and the frame holds
            # the aggregate, whose gaps are PII (Constitution §8).
            if constraint == _USER_FK:
                raise UserNotFound(f"the owner of fit score {score.id.value} is gone") from None
            if constraint == _GUEST_FK:
                raise GuestSessionExpired(
                    f"the guest session owning fit score {score.id.value} is gone"
                ) from None
            raise

    async def find_for_pair(
        self, owner: Owner, cv_fingerprint: CvFingerprint, posting_fingerprint: PostingFingerprint
    ) -> FitScore | None:
        return (
            await self._session.execute(
                select(FitScore).where(
                    _owned_by(owner),
                    _table.c.cv_fingerprint == cv_fingerprint,
                    _table.c.posting_fingerprint == posting_fingerprint,
                )
            )
        ).scalar_one_or_none()

    async def scored_at_since(self, owner: Owner, since: datetime | None) -> Sequence[datetime]:
        """Exclusive and oldest first (the port's docstring). `None` counts every score."""
        query = select(_table.c.scored_at).where(_owned_by(owner)).order_by(_table.c.scored_at)
        if since is not None:
            query = query.where(_table.c.scored_at > since)
        return (await self._session.execute(query)).scalars().all()


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance. Never executed.
    def _assert_implements_fit_score_repository(adapter: SqlAlchemyFitScoreRepository) -> None:
        _: FitScoreRepository = adapter
