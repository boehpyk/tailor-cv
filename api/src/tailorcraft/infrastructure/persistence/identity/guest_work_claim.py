"""`SqlAlchemyGuestWorkClaim` — the `GuestWorkClaimPort` adapter (slice 2.4, ADR-0025, technical plan
§0.2, §3).

A claim moves every row a guest session owns to a user, in one transaction. The invariant (*every row
of a session changes owner together*) spans five contexts' tables, so it is held here, by one
transaction over Core statements, and not by any aggregate (ADR-0025 decision 8, ADR-0018's argument
for the purge). After `lock_session`, `transfer` runs exactly these, in this order:

```sql
UPDATE intake_base_cv      SET guest_session_id = NULL, user_id = :u
 WHERE guest_session_id = :s AND copied_from_base_cv_id IS NULL;            -- not the working copies
UPDATE posting_job_posting SET guest_session_id = NULL, user_id = :u WHERE guest_session_id = :s;
UPDATE tailoring_run       SET guest_session_id = NULL, user_id = :u WHERE guest_session_id = :s;
UPDATE export_job          SET guest_session_id = NULL, user_id = :u WHERE guest_session_id = :s;
UPDATE scoring_fit_score g SET guest_session_id = NULL, user_id = :u
 WHERE g.guest_session_id = :s
   AND g.base_cv_id NOT IN (SELECT id FROM intake_base_cv                   -- not a working copy's
                             WHERE guest_session_id = :s AND copied_from_base_cv_id IS NOT NULL)
   AND NOT EXISTS (SELECT 1 FROM scoring_fit_score u                        -- not a pair the user has
                    WHERE u.user_id = :u AND u.cv_fingerprint = g.cv_fingerprint
                      AND u.posting_fingerprint = g.posting_fingerprint);
DELETE FROM intake_base_cv WHERE guest_session_id = :s RETURNING file_key;   -- what is left: copies
DELETE FROM identity_guest_session WHERE id = :s;                            -- exactly one row
```

- **One statement sets both owner columns**, so `ck_<table>_exactly_one_owner` never sees an
  intermediate state, not even inside the transaction.
- **No row is copied and no id changes**; storage keys derive from ids (ADR-0011 §1), so **no file
  moves**. The only files touched are the dropped working copies', and not here: their keys come back
  in `ClaimedGuestWork.files_to_unlink` for the use case to unlink after the commit.
- **A working copy is never claimed** (§0.9, ADR-0022 amendment (d)): it is a copy of a CV some
  account already keeps, possibly another account's. Nothing references `intake_base_cv` by foreign
  key (`tailoring_run.base_cv_id` has none, 2.3), so the copies' `DELETE` cascades nowhere, and a run
  made from one is claimed and dangles — history derives "CV deleted" at read time. A base CV has
  exactly one file, its `file_key`; export files hang off export jobs, which are claimed, never off
  a base CV, so there is no derived key to collect here.
- **A fit score moves unless it is a working copy's or a twin** (slice 5.1, AC-34). Both leftovers go
  with the session's `DELETE`, by cascade, so they need no statement. The working-copy clause relies
  on the order above: the copies are the session's only copied rows until their own `DELETE`.
- **A twin that commits during the claim** is invisible to `NOT EXISTS`, so
  `uq_scoring_fit_score_user_pair` fires. That is not a user FK, so it propagates: the router rolls the
  whole claim back and `main.py`'s `SQLAlchemyError` handler answers the generic 503 (S-31). A retry
  sees the twin and skips it. Locking the user row would stall every insert for that user to beat a
  same-pair, same-instant collision.
- **The session's `DELETE` is the purge's signal** (§0.5): the purge unlinks a session's files only
  when its own `DELETE` removed the row, and after this commits it removes nothing.

**Why no `version` is bumped — three facts, and the third is load-bearing** (§0.7, ADR-0025). A run or
an export job may be `queued`/`running`/`rendering` while it changes owner, and the claim neither
waits for nor refuses it:

1. **The worker never authorizes**: `ExecuteTailoringRun` and `RenderExportJob` load by id (2.3 §0.3),
   so an owner change cannot make it refuse its own job.
2. **The worker never writes an owner column**: the ORM's `UPDATE` sets only dirty attributes, and an
   outcome dirties no owner attribute, so the worker's save cannot write the guest back.
3. **The claim never bumps `version`**: so the worker's `UPDATE … WHERE version = :loaded` still
   matches after this commits. Bumping it "for safety" loses a paid result: the worker's outcome save
   raises `TailoringRunConcurrentlyModified` (AC-18's mutation, observed). The `UPDATE`s below name the owner columns and nothing else, on purpose.

**Lock order** (§0.6): `lock_session` takes the session row `FOR UPDATE`; `transfer`'s `UPDATE`s take
the user row `FOR KEY SHARE` implicitly, through the `user_id` FK check. A guest `INSERT` waits behind
(1) and then fails its guest FK (T15's translation); account erasure's `FOR UPDATE` on the user meets
(2), and if it committed first the FK refuses — `UserNotFound`, recognised by constraint **name**,
never by message (`database.violated_constraint`).

**Nothing here commits and nothing here logs.** `CommittingGuestWorkClaim`
(`infrastructure/identity/claim_access.py`) commits after `transfer`; the router logs the counts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy import delete, exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.util import identity_key

from tailorcraft.domain.identity.claim import ClaimedGuestWork
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.scoring.fit_score import fit_score_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)

if TYPE_CHECKING:
    from tailorcraft.domain.identity.ports import GuestWorkClaimPort

# Recognised by name, never by message (`violated_constraint`). The `fk_<table>_user_id_identity_user`
# names are `registry.py`'s convention, and each is already load-bearing in its
# repository's `add`; renaming one is a breaking change here too.
_USER_FKS: Final = frozenset(
    {
        "fk_intake_base_cv_user_id_identity_user",
        "fk_posting_job_posting_user_id_identity_user",
        "fk_tailoring_run_user_id_identity_user",
        "fk_export_job_user_id_identity_user",
        "fk_scoring_fit_score_user_id_identity_user",
    }
)


class SqlAlchemyGuestWorkClaim:
    """Everything a claim asks of the store of record, over one `AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_session(self, token_hash: str) -> GuestSession | None:
        """`SELECT … FROM identity_guest_session WHERE token_hash = :h FOR UPDATE`, through the ORM
        (it returns the aggregate whose `is_expired` the use case applies) — `None` when absent.

        The lock is held until the transaction ends: `CommittingGuestWorkClaim.transfer`'s commit,
        or the router's rollback on every path that does not transfer (no session, expired). A
        second claim or the purge waiting on this row then finds it gone.

        Filtered on the table's column, not on `GuestSession.token_hash`, which is a read-only
        property and would build no predicate (`repositories/identity/guest_session.py` explains).
        """
        result = await self._session.execute(
            select(GuestSession)
            .where(guest_session_table.c.token_hash == token_hash)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def transfer(self, session_id: GuestSessionId, user_id: UserId) -> ClaimedGuestWork:
        """The module docstring's seven statements; counts from `rowcount`, keys from `RETURNING`.

        Raises `UserNotFound` when a `user_id` FK refuses a re-key (the account was erased first);
        the caller rolls the transaction back and nothing has moved. Raises `InvariantViolated` if
        the session's `DELETE` does not remove exactly one row — we hold its lock, so anything else
        is a bug, and it is loud rather than a claim reported as done.
        """
        connection = await self._session.connection()
        try:
            base_cvs = await connection.execute(
                update(base_cv_table)
                .where(
                    base_cv_table.c.guest_session_id == session_id,
                    base_cv_table.c.copied_from_base_cv_id.is_(None),
                )
                .values(guest_session_id=None, user_id=user_id)
            )
            job_postings = await connection.execute(
                update(job_posting_table)
                .where(job_posting_table.c.guest_session_id == session_id)
                .values(guest_session_id=None, user_id=user_id)
            )
            tailoring_runs = await connection.execute(
                update(tailoring_run_table)
                .where(tailoring_run_table.c.guest_session_id == session_id)
                .values(guest_session_id=None, user_id=user_id)
            )
            export_jobs = await connection.execute(
                update(export_job_table)
                .where(export_job_table.c.guest_session_id == session_id)
                .values(guest_session_id=None, user_id=user_id)
            )
            users_score = fit_score_table.alias("users_score")
            fit_scores = await connection.execute(
                update(fit_score_table)
                .where(
                    fit_score_table.c.guest_session_id == session_id,
                    fit_score_table.c.base_cv_id.not_in(
                        select(base_cv_table.c.id).where(
                            base_cv_table.c.guest_session_id == session_id,
                            base_cv_table.c.copied_from_base_cv_id.is_not(None),
                        )
                    ),
                    ~exists().where(
                        users_score.c.user_id == user_id,
                        users_score.c.cv_fingerprint == fit_score_table.c.cv_fingerprint,
                        users_score.c.posting_fingerprint == fit_score_table.c.posting_fingerprint,
                    ),
                )
                .values(guest_session_id=None, user_id=user_id)
            )
        except IntegrityError as exc:
            if violated_constraint(exc) in _USER_FKS:
                # `from None`: the listener already reduced the chain to identifiers.
                raise UserNotFound(f"no User with id {user_id!r} to claim into") from None
            raise

        # What is left on the session is its working copies: dropped, never claimed (§0.9).
        dropped_keys: list[FileRef] = list(
            (
                await connection.execute(
                    delete(base_cv_table)
                    .where(base_cv_table.c.guest_session_id == session_id)
                    .returning(base_cv_table.c.file_key)
                )
            )
            .scalars()
            .all()
        )

        # The `GuestSession` `lock_session` loaded leaves the identity map before its row goes: a
        # Core `DELETE` does not tell the ORM, and a later flush of that instance would target a row
        # that no longer exists (2.2's `delete_account` precedent). By identity — never a load.
        stale = self._session.identity_map.get(identity_key(GuestSession, session_id))
        if stale is not None:
            self._session.expunge(stale)

        session_deleted = await connection.execute(
            delete(guest_session_table).where(guest_session_table.c.id == session_id)
        )
        if session_deleted.rowcount != 1:
            # No session id in the message: a 500's message reaches Sentry, and the claim never
            # names the session it took (the router's log line counts, it does not identify).
            raise InvariantViolated(
                f"a claim deleted {session_deleted.rowcount} guest session rows, not 1"
            )

        return ClaimedGuestWork(
            base_cvs=base_cvs.rowcount,
            job_postings=job_postings.rowcount,
            tailoring_runs=tailoring_runs.rowcount,
            export_jobs=export_jobs.rowcount,
            fit_scores=fit_scores.rowcount,
            working_copies_dropped=len(dropped_keys),
            files_to_unlink=tuple(dropped_keys),
        )


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance. Never executed.
    def _assert_implements_guest_work_claim(adapter: SqlAlchemyGuestWorkClaim) -> None:
        _: GuestWorkClaimPort = adapter
