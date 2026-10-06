"""The `tracking_application` table and the imperative mapping for `TrackedApplication` (ADR-0007,
ADR-0029, slice 3.1).

As in every other mapping module, each column collides with a read-only `@property` of the same short
name on the aggregate (`id`, `stage`, `title`, …), so `properties=` targets the **private** attributes
(`_id`, `_stage`, `_title`, …) that `track`, `move_to` and `retitle` write. Renaming one of them in
`domain/tracking/tracked_application.py` is a breaking change to this module, and the aggregate says
so. `_recorded_events` (from `RecordsEvents`) is deliberately absent: an in-memory outbox, not a fact
about the card.

**Never `class TrackedApplication(Base)`, never `mapped_column` on the domain class** (ADR-0002).

**`version` is the aggregate's number, not the mapper's** — `version_id_col` with
`version_id_generator=False`, exactly as `tailoring_run` (ADR-0015 §3). The aggregate bumps `_version`
on every effective change (TA-4), and the flush's `UPDATE … WHERE id = :id AND version = :loaded`
refuses a write whose row moved on or vanished since the load: `StaleDataError`, which the repository
translates into `TrackedApplicationConcurrentlyModified` and nothing above it ever sees. With the
default generator SQLAlchemy would bump the column itself, and the domain's `version` and the row's
would drift apart by one on every save.

**The constraints mirror the aggregate's invariants**, so a hand-written `UPDATE` cannot store what
the domain refuses: `stage_known` (TA-2), `title_length` (TA-5's bound — the control-character rule
stays in `ApplicationTitle` and is re-checked on every load by `ApplicationTitleType`), and
`stage_changed_after_tracked` (TA-3, as `tailoring_run`'s timestamp CHECKs mirror TR-2).

The authoritative migration is `alembic/versions/…_add_tracking_application.py`; this `Table` must
stay identical to it, which `alembic check` (autogenerate producing nothing) proves.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, Integer, Table, text
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tailorcraft.domain.tracking.tracked_application import TrackedApplication
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.registry import mapper_registry, metadata
from tailorcraft.infrastructure.persistence.types.identity import UserIdType
from tailorcraft.infrastructure.persistence.types.tracking import (
    ApplicationStageType,
    ApplicationTitleType,
    TrackedApplicationIdType,
    TrackedRunRefType,
)

tracked_application_table = Table(
    "tracking_application",
    metadata,
    # Application-assigned UUIDv7 (ADR-0007): no server default.
    Column("id", TrackedApplicationIdType, primary_key=True),
    # **One `NOT NULL` user column, and no `guest_session_id` — the deliberate contradiction of
    # ADR-0022** (plan §0.5, ADR-0029). Every other owned table since 2.2 stores the sum type
    # `GuestOwner | UserOwner` as two nullable FKs plus `ck_<table>_exactly_one_owner`. A card can
    # never be a guest's (the use case refuses one), so that shape would store an arm nothing may
    # ever write, and the 24-hour purge would have to be *trusted* not to reach a card instead of
    # being unable to. Consequences, all for free: the purge (which deletes by guest FK) cannot reach
    # this table; 2.4's claim has nothing to re-key here; account erasure is this `ON DELETE CASCADE`
    # under the user-row lock erasure already takes. Do not "fix" this to match the other tables.
    #
    # The FK's check is also a lock point: an `INSERT` takes the user row `FOR KEY SHARE`, so a card
    # racing an erasure (user row `FOR UPDATE`) waits and is then refused on
    # `fk_tracking_application_user_id_identity_user` (AC-19). Not separately indexed: the leading
    # column of `ix_tracking_application_user_id_stage_changed_at` serves the cascade.
    Column(
        "user_id",
        UserIdType,
        ForeignKey(user_table.c.id, ondelete="CASCADE"),
        nullable=False,
    ),
    # **No foreign key to `tailoring_run`** (ADR-0014/0016/0023: contexts' tables are not fused).
    # The race an FK would close — a track landing after the run's deletion — is closed by two locks
    # in `SqlAlchemyTrackedApplicationRepository.add` and the history deletion (plan §0.7).
    # Unique (`uq_tracking_application_tailoring_run_id`): one card per run. A run has exactly one
    # owner, so "per run" is also "per (user, run)"; the unique index also serves `find_for_run` and
    # the history deletion's card `DELETE`.
    Column("tailoring_run_id", TrackedRunRefType, nullable=False, unique=True),
    Column("stage", ApplicationStageType, nullable=False),
    Column("title", ApplicationTitleType, nullable=True),
    Column("tracked_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    Column("stage_changed_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    # The mapper's `version_id_col` with `version_id_generator=False` (module docstring). The server
    # default exists for hand-written inserts only; every ORM insert writes the aggregate's 1.
    Column("version", Integer, nullable=False, server_default=text("1")),
    # TA-2. Listed in board order, as `ApplicationStage` declares them; a new member is a migration.
    CheckConstraint(
        "stage IN ('to_apply','applied','interviewing','offer','rejected','withdrawn')",
        name="stage_known",
    ),
    # TA-5's bound, in the unit `ApplicationTitle` counts (code points, after trimming).
    CheckConstraint(
        "title IS NULL OR char_length(title) BETWEEN 1 AND 120",
        name="title_length",
    ),
    # TA-3.
    CheckConstraint("stage_changed_at >= tracked_at", name="stage_changed_after_tracked"),
    # The board query's `WHERE user_id = :u ORDER BY stage_changed_at DESC, id DESC` (plan §0.8,
    # AC-31's `EXPLAIN`); its leading column also serves `count_for_user` and the FK cascade. The
    # `id DESC` tiebreak: whole-second timestamps make same-second moves ordinary, and a UUIDv7's
    # byte order is time order one resolution down (`tailoring_run`'s history index, same reason).
    Index(
        "ix_tracking_application_user_id_stage_changed_at",
        "user_id",
        text("stage_changed_at DESC"),
        text("id DESC"),
    ),
)

mapper_registry.map_imperatively(
    TrackedApplication,
    tracked_application_table,
    properties={
        "_id": tracked_application_table.c.id,
        # A `UserId`, never an `Owner` — see the `user_id` column's comment above.
        "_user_id": tracked_application_table.c.user_id,
        "_tailoring_run_id": tracked_application_table.c.tailoring_run_id,
        "_stage": tracked_application_table.c.stage,
        "_title": tracked_application_table.c.title,
        "_tracked_at": tracked_application_table.c.tracked_at,
        "_stage_changed_at": tracked_application_table.c.stage_changed_at,
        "_version": tracked_application_table.c.version,
    },
    version_id_col=tracked_application_table.c.version,
    version_id_generator=False,
)
