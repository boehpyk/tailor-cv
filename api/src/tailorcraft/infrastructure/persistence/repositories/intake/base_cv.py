"""`SqlAlchemyBaseCvRepository` — the `BaseCvRepository` port (ADR-0007).

Filters below query `BaseCv._id` / `BaseCv._owner_guest_session_id`, the **private** attributes the
imperative mapping in `infrastructure/persistence/mapping/intake/base_cv.py` targets — never
`BaseCv.id` / `BaseCv.owner`. Those short names are plain read-only `@property` objects on
the domain class, not `InstrumentedAttribute`s: `select(BaseCv).where(BaseCv.id == x)` would call the
property, get back a `BaseCvId`, evaluate a bare Python `==` against `x`, and build
`select(...).where(True)` or `.where(False)` — a predicate that matches everything or nothing,
silently, with no error anywhere. It looks like a typo the first time you see it; it is not one. See
the identical note in `repositories/identity/guest_session.py`.

**The two writes a request makes to a saved CV — `save_label` and `remove` — are Core statements with
the owner in the `WHERE`**, never an ORM flush of the loaded aggregate (slice 2.2, technical plan §3).
An ORM flush writes `WHERE id = :id` and nothing else, so an ownership check made before it is a
check-then-act; with the owner in the statement, the check and the write are one atomic predicate,
and the row count is the verdict.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Final, assert_never, cast

from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.orm.attributes import instance_state, set_committed_value
from sqlalchemy.orm.util import identity_key

from tailorcraft.domain.identity.errors import GuestSessionNotFound, UserNotFound
from tailorcraft.domain.identity.ownership import GuestOwner, Owner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.intake.saved_base_cv_summary import SavedBaseCvSummary
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table

if TYPE_CHECKING:
    from tailorcraft.domain.intake.ports import BaseCvRepository

# `BaseCv._id` etc. are class-body annotations only (`domain/intake/base_cv.py` sets no value —
# `map_imperatively`'s `properties=` installs the real `InstrumentedAttribute` at import time), so
# mypy sees them typed as the *domain* type (`BaseCvId`, `datetime`, …), not as SQLAlchemy's mapped
# attribute. `BaseCv._id == cv_id` then type-checks as `BaseCvId.__eq__`, returning a plain `bool` —
# which is exactly the runtime trap the module docstring describes, just caught by mypy instead of a
# silently-empty query. These `cast`s tell mypy what is actually there at runtime without touching
# behaviour; each is a `cast`, not an `Any`, so CLAUDE.md's ban on unjustified `Any` does not apply.
_BASE_CV_GUEST_SESSION_ID: InstrumentedAttribute[GuestSessionId | None] = cast(
    "InstrumentedAttribute[GuestSessionId | None]", BaseCv._owner_guest_session_id
)
_BASE_CV_USER_ID: InstrumentedAttribute[UserId | None] = cast(
    "InstrumentedAttribute[UserId | None]", BaseCv._owner_user_id
)
_BASE_CV_UPLOADED_AT: InstrumentedAttribute[datetime] = cast(
    "InstrumentedAttribute[datetime]", BaseCv._uploaded_at
)

# Recognised by name, never by message (`violated_constraint`). Renaming the FK in
# `mapping/intake/base_cv.py` is a breaking change to `add` below.
_USER_FK: Final = "fk_intake_base_cv_user_id_identity_user"
# The guest twin (slice 2.4, AC-12): a claim that commits between a guest write's session lookup and
# its `INSERT` deletes the session row, and this FK refuses. That refusal *is* "the session is gone"
# — the same fact the cookie resolver reports — so it becomes the same error and the same 401
# `guest_session_expired`. Name checked against `registry.py`'s convention and `pg_constraint`.
_GUEST_FK: Final = "fk_intake_base_cv_guest_session_id_identity_guest_session"


def _owner_predicate(owner: Owner) -> ColumnElement[bool]:
    """The owner as a `WHERE` clause: one owner column compared, the other implied by the CHECK.

    Part of *what is written*, not a check made beforehand — so a row that changed hands, or went,
    between the load and the statement matches nothing, and the statement's row count says so.
    """
    match owner:
        case GuestOwner(guest_session_id=guest_session_id):
            return base_cv_table.c.guest_session_id == guest_session_id
        case UserOwner(user_id=user_id):
            return base_cv_table.c.user_id == user_id
        case _:
            assert_never(owner)


class SqlAlchemyBaseCvRepository:
    """Persistence for `BaseCv`, backed by `intake_base_cv`.

    Hides its `AsyncSession` completely — nothing outside this module ever sees it (ADR-0007). The
    unit of work (commit/rollback) is the caller's concern, opened once per request in
    `infrastructure/api/deps.py::get_session`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        # Strong references to every aggregate `add` or `get` handed out, for this repository's
        # lifetime — one request (`deps.py`) or one task (`tasks/container.py`), never longer. The
        # session's identity map holds only **weak** references, so an aggregate its caller dropped
        # is collected at once and the next `get` of that id is a miss that decodes the whole
        # extracted text again, on the loop (T30b-A). The copy route does exactly that twice: the
        # router authorizes the source and discards it, and the use case returns only the copy's id,
        # which the router then re-reads. Pinning makes both an identity-map hit. It changes no
        # answer: `get` still asks the database whether the row exists.
        self._pinned: list[BaseCv] = []

    def next_identity(self) -> BaseCvId:
        """Synchronous: application-assigned UUIDv7 needs no I/O (ADR-0007)."""
        return BaseCvId(uuid7())

    async def add(self, cv: BaseCv) -> None:
        """Insert `cv`; raise `UserNotFound` if its `UserOwner` no longer exists (S-12, AC-32).

        `flush()`, not `commit()`: the transaction boundary belongs to the caller (a request or a
        task), not to the repository.

        **The race this translates.** An account upload checks the user exists, writes the file,
        then inserts. An erasure that lands in between locks the `identity_user` row `FOR UPDATE`
        (`SqlAlchemyAccountData.files_of_account`); this `INSERT`'s FK check needs `FOR KEY SHARE`
        on the same row, so it waits, and once the erasure commits the row is gone and the FK
        refuses. That refusal *is* "the user is gone" — the same fact `resolve_existing_user`
        reports — so it becomes the same error, and the route answers the same 401. The file already
        written is an orphan for the sweep, by design (ADR-0006 §2).

        **Inside a SAVEPOINT**, as `SqlAlchemyUserRepository.add` is and for its reason: a failed
        flush at the root expires every instance in the session inside the flush (the 1.4 lesson),
        while a SAVEPOINT confines the rollback to this one pending `BaseCv`, and the route can still
        answer with a usable session. `add` goes *inside* the `async with`, since `begin_nested()`
        flushes on entry. The guest FK is the same shape for the other owner (slice 2.4, AC-12): a
        claim that deleted the session row first makes it refuse, and that is "the session is gone",
        `GuestSessionNotFound`. Any other refusal — `uq_intake_base_cv_file_key`, a CHECK —
        propagates untranslated.
        """
        # Read before the flush: after a nested rollback the pending `cv` is expunged, and the
        # message below must not depend on whether its attributes survived that.
        cv_id = cv.id
        try:
            async with self._session.begin_nested():
                self._session.add(cv)
                await self._session.flush()
            self._pinned.append(cv)
        except IntegrityError as exc:
            if violated_constraint(exc) == _USER_FK:
                # `from None`: the listener already reduced the chain to identifiers, and the frame
                # holds `cv`, whose extracted text is a CV (Constitution §8).
                raise UserNotFound(f"the owner of {cv_id!r} no longer exists") from None
            if violated_constraint(exc) == _GUEST_FK:
                # `from None`: the chain is already reduced to identifiers, and the frame holds
                # the aggregate (Constitution §8). AC-12, slice 2.4.
                raise GuestSessionNotFound(
                    f"the guest session owning {cv_id!r} no longer exists"
                ) from None
            raise

    async def get(self, cv_id: BaseCvId) -> BaseCv:
        """The aggregate, through the identity map; `BaseCvNotFound` if the **row** is gone.

        **Why not `select(BaseCv)`** (T30b-A): its result processing runs every column's
        `TypeDecorator` — `ExtractedText`'s validation over up to millions of characters, on the
        event loop — even when the identity is already loaded and the fresh value is discarded (a
        plain `select` does not overwrite a loaded instance). A copy loaded its source twice.

        **Why not `session.get` alone:** on an identity-map hit it asks the database nothing, so a
        row deleted since the load — by a concurrent request, or a Core `DELETE` in this very
        session — would still be "found". Callers rely on `get` knowing: the copy's re-read after
        `StoredFileMissing` is how a concurrent delete (S-33, 404) is told apart from a lost file
        (S-32, 410). So a hit is confirmed by an id-only probe, which never selects the text: the
        same answer about existence `select(BaseCv)` gave, without re-decoding the rest.

        A miss, and an **expired** instance (after a failed flush or a rolled-back SAVEPOINT), go
        through `session.get`, which loads fresh — and answers `None` for an expired instance whose
        row is gone. An expunged instance (`save_label`, `remove`) is not in the map, so it is a
        miss and loads fresh too.
        """
        # The identity map is untyped (`Any`); the key names the class, so the value is a `BaseCv`.
        cached = cast("BaseCv | None", self._session.identity_map.get(identity_key(BaseCv, cv_id)))
        if cached is not None and not instance_state(cached).expired:
            probe = await self._session.execute(
                select(base_cv_table.c.id).where(base_cv_table.c.id == cv_id)
            )
            if probe.first() is None:
                raise BaseCvNotFound(f"no BaseCv with id {cv_id!r}")
            return cached
        found = await self._session.get(BaseCv, cv_id)
        if found is None:
            raise BaseCvNotFound(f"no BaseCv with id {cv_id!r}")
        self._pinned.append(found)
        return found

    async def list_for_session(self, sid: GuestSessionId) -> Sequence[BaseCv]:
        """Newest first — the sensible default for a UI list (`GET /api/base-cvs`): a guest who has
        just uploaded a second CV expects to see it above the first, not have to scroll for it."""
        result = await self._session.execute(
            select(BaseCv)
            .where(_BASE_CV_GUEST_SESSION_ID == sid)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
            .order_by(_BASE_CV_UPLOADED_AT.desc())
        )
        return result.scalars().all()

    async def count_for_session(self, sid: GuestSessionId) -> int:
        """`SELECT count(*)`, not `len(await list_for_session(sid))` — the port docstring is explicit
        that this must not materialize every row just to measure them."""
        result = await self._session.execute(
            select(func.count()).select_from(BaseCv).where(_BASE_CV_GUEST_SESSION_ID == sid)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
        )
        return result.scalar_one()

    async def list_for_user(self, uid: UserId) -> Sequence[SavedBaseCvSummary]:
        """Newest first, like `list_for_session`, as `SavedBaseCvSummary` rows, and **without ever
        selecting `extracted_text`** (AC-52, technical plan §3 amendment 2026-09-25).

        A Core `SELECT` over `base_cv_table`'s named columns, not `select(BaseCv)`: the ORM would
        build aggregates, and an aggregate carries its text. `character_count` is computed by
        Postgres — `char_length` counts **code points**, which is what Python's `len` counts, so the
        list and `ExtractedText.character_count` agree on a CV full of non-ASCII names — and is
        `NULL` exactly when `extracted_text` is (a CV not yet, or never, extracted). The column
        appears inside the function call and nowhere in the select list, so no byte of the text
        leaves the database.

        The columns keep their `TypeDecorator`s, so each row arrives already as the domain's value
        objects (`BaseCvId`, `BaseCvLabel`, `BaseCvStatus`, …) — the same translation `get` gets,
        not a second one written here.
        """
        c = base_cv_table.c
        result = await self._session.execute(
            select(
                c.id,
                c.label,
                c.original_filename,
                c.content_type,
                c.size_bytes,
                c.status,
                func.char_length(c.extracted_text).label("character_count"),
                c.extraction_failure_reason,
                c.uploaded_at,
            )
            .where(c.user_id == uid)
            # `id` breaks a tie within one whole second (`uploaded_at` is whole-second by the
            # `Clock`'s contract): a UUIDv7 is time-ordered, so it agrees with "newest first" and
            # makes the order total rather than whatever the planner returns that day.
            .order_by(c.uploaded_at.desc(), c.id.desc())
        )
        return [
            SavedBaseCvSummary(
                id=row.id,
                label=row.label,
                original_filename=row.original_filename,
                content_type=row.content_type,
                size_bytes=row.size_bytes,
                status=row.status,
                character_count=row.character_count,
                failure_reason=row.extraction_failure_reason,
                uploaded_at=row.uploaded_at,
            )
            for row in result
        ]

    async def count_for_user(self, uid: UserId) -> int:
        """`SELECT count(*)` over `ix_intake_base_cv_user_id`, for the same reason
        `count_for_session` never materializes the rows it counts."""
        result = await self._session.execute(
            select(func.count()).select_from(BaseCv).where(_BASE_CV_USER_ID == uid)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
        )
        return result.scalar_one()

    async def save_label(self, cv: BaseCv) -> None:
        """`UPDATE intake_base_cv SET label = :label WHERE id = :id AND <owner>` — Core, the label
        only, the owner in the `WHERE`. Zero rows → `BaseCvNotFound` (a concurrent delete won).

        **The aggregate leaves the session first.** `BaseCv.rename` made it dirty, so any flush —
        the handler's commit — would emit the ORM's own `UPDATE … WHERE id = :id`, with no owner
        predicate, and against a row a concurrent delete removed it would raise `StaleDataError`
        into a request that should be a 404. The same trap `SqlAlchemyLoginRepository.save_rotation`
        documents, with the same fix: `expunge`, write in Core, and on success re-attach the object
        **clean** (`set_committed_value` — the label *is* what the row now holds), so a later flush
        sends nothing and a later read in this session sees the new label. On a refusal it stays
        detached; the caller answers 404 and touches it no further.

        No SAVEPOINT: an `UPDATE` matching nothing is not a database error, so there is nothing to
        roll back to. Values are read into locals before the statement.
        """
        cv_id, owner, label = cv.id, cv.owner, cv.label
        was_attached = cv in self._session
        if was_attached:
            self._session.expunge(cv)

        connection = await self._session.connection()
        result = await connection.execute(
            update(base_cv_table)
            .where(base_cv_table.c.id == cv_id, _owner_predicate(owner))
            .values(label=label)
        )
        if result.rowcount == 0:
            raise BaseCvNotFound(f"no BaseCv with id {cv_id!r} for its owner")

        set_committed_value(cv, "_label", label)
        if was_attached:
            self._session.add(cv)

    async def remove(self, cv_id: BaseCvId, owner: UserOwner) -> None:
        """`DELETE FROM intake_base_cv WHERE id = :id AND user_id = :owner` — Core, the owner part
        of *what is removed*. Zero rows → `BaseCvNotFound`: no such row, not this owner's, or a
        concurrent delete won. Only the winner's use case goes on to unlink the file (AC-10, AC-27).

        **Any `BaseCv` for this id leaves the identity map first.** The use case loaded it through
        `get`, so it is attached; after a Core `DELETE` the ORM would not know the row went, and a
        later flush of that instance (the committing adapter's commit, or a `begin_nested()` entry
        flush) would issue an `UPDATE` or `DELETE` of its own, without the owner predicate, against a
        row that no longer exists. Expunging an object this method was not handed — looked up by
        identity, as `SqlAlchemyLoginRepository.remove` does — keeps that true whoever loaded it.

        Rows only; the file is the caller's, after the removal is durable (ADR-0006 §2). The
        durability is not this method's either: `CommittingBaseCvRemoval` commits (technical plan
        §0.4).
        """
        stale = self._session.identity_map.get(identity_key(BaseCv, cv_id))
        if stale is not None:
            self._session.expunge(stale)

        connection = await self._session.connection()
        result = await connection.execute(
            delete(base_cv_table).where(base_cv_table.c.id == cv_id, _owner_predicate(owner))
        )
        if result.rowcount == 0:
            raise BaseCvNotFound(f"no BaseCv with id {cv_id!r} for its owner")


if TYPE_CHECKING:
    # Makes mypy prove `SqlAlchemyBaseCvRepository` structurally satisfies `BaseCvRepository` rather
    # than trusting the shape by eye. Never executed — a `Protocol` needs no instance to check
    # against, only a compatible signature — so it costs nothing at runtime.
    def _assert_implements_base_cv_repository(repo: SqlAlchemyBaseCvRepository) -> None:
        _: BaseCvRepository = repo
