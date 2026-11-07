"""The `billing_credit_account` and `billing_credit_entry` tables, and the imperative mapping of
`CreditAccount` and its `CreditEntry`s onto them (ADR-0007, ADR-0042, plan §3). **Never
`class CreditAccount(Base)`.**

**The balance is not a column.** It is `SUM(credits)` over the account's entries (plan §0.2), so the
database holds the history and the aggregate derives the rest. The concurrency half of the money rules
lives here, beside the aggregate's in-memory checks:

- `uq_billing_credit_entry_charge_run` / `_refund_run`: at most one charge and one refund per run
  (CR-3, CR-4), even for two transactions that both passed the in-memory check.
- `uq_billing_credit_entry_opening`: one opening entry per account (CR-1).
- The account row is what `lock_for_charge` locks (`FOR NO KEY UPDATE`).

**`tailoring_run_id` has no foreign key** (ADR-0014's cross-context rule): billing names a run, it never
owns one, and a history entry's deletion must not take the money record with it.

**The mapping.** `properties=` targets the private attributes the named constructors write (each column
would collide with a read-only `@property` of the same short name). `_entries` is a `selectin`
relationship ordered oldest first, so the aggregate's `_entries[0]` is the opening entry (CR-1). Its
cascade is the default `save-update, merge`, **no `delete-orphan`**: the aggregate never removes an entry
(CR-6), and `passive_deletes=True` leaves erasure to the database's `ON DELETE CASCADE`. An entry's
`user_id` is mapped as `_user_id` and filled by the relationship on flush; the domain never reads it.
`_pending` and `_recorded_events` are absent: in-memory buffers, not facts. Both classes keep their
explicit empty `__init__` (CLAUDE.md: the default one `map_imperatively` installs would accept the mapped
names and skip every invariant).

The authoritative migration is `alembic/versions/…_add_billing_and_demand_tables.py`; the
autogenerate-against-head tests prove these `Table`s match it.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, ForeignKey, Index, SmallInteger, Table, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import relationship

from tailorcraft.domain.billing.credit_account import CreditAccount, CreditEntry
from tailorcraft.domain.billing.value_objects import CreditEntryKind
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.registry import mapper_registry, metadata
from tailorcraft.infrastructure.persistence.types.billing import (
    ChargedRunRefType,
    CreditEntryIdType,
    CreditEntryKindType,
)
from tailorcraft.infrastructure.persistence.types.identity import UserIdType


def _sql_list(*kinds: CreditEntryKind) -> str:
    return ", ".join(f"'{kind.value}'" for kind in kinds)


_OPENING = _sql_list(CreditEntryKind.WELCOME, CreditEntryKind.EXISTING_ACCOUNT)

credit_account_table = Table(
    "billing_credit_account",
    metadata,
    Column(
        "user_id",
        UserIdType,
        ForeignKey(user_table.c.id, ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("opened_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    Column("first_pack_click_at", TIMESTAMP(timezone=True, precision=0), nullable=True),
    Column("payment_notice_opted_in_at", TIMESTAMP(timezone=True, precision=0), nullable=True),
)

credit_entry_table = Table(
    "billing_credit_entry",
    metadata,
    Column("id", CreditEntryIdType, primary_key=True),
    Column(
        "user_id",
        UserIdType,
        ForeignKey(credit_account_table.c.user_id, ondelete="CASCADE"),
        nullable=False,
    ),
    Column("kind", CreditEntryKindType, nullable=False),
    Column("credits", SmallInteger, nullable=False),
    Column("tailoring_run_id", ChargedRunRefType, nullable=True),
    Column("created_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    CheckConstraint(f"kind IN ({_sql_list(*CreditEntryKind)})", name="kind_known"),
    # CE-1.
    CheckConstraint(
        f"(kind IN ({_OPENING}, 'refund') AND credits = 1)"
        " OR (kind = 'grant' AND credits BETWEEN 1 AND 100)"
        " OR (kind = 'charge' AND credits = -1)",
        name="credits_by_kind",
    ),
    # CE-2.
    CheckConstraint(
        "(tailoring_run_id IS NOT NULL) = (kind IN ('charge', 'refund'))",
        name="run_iff_charge_or_refund",
    ),
    # The history, newest first; also serves the account FK's cascade.
    Index(
        "ix_billing_credit_entry_user_id_created_at",
        "user_id",
        text("created_at DESC"),
        text("id DESC"),
    ),
    Index(
        "uq_billing_credit_entry_charge_run",
        "tailoring_run_id",
        unique=True,
        postgresql_where=text("kind = 'charge'"),
    ),
    Index(
        "uq_billing_credit_entry_refund_run",
        "tailoring_run_id",
        unique=True,
        postgresql_where=text("kind = 'refund'"),
    ),
    Index(
        "uq_billing_credit_entry_opening",
        "user_id",
        unique=True,
        postgresql_where=text(f"kind IN ({_OPENING})"),
    ),
)

mapper_registry.map_imperatively(
    CreditEntry,
    credit_entry_table,
    properties={
        "_id": credit_entry_table.c.id,
        "_user_id": credit_entry_table.c.user_id,
        "_kind": credit_entry_table.c.kind,
        "_credits": credit_entry_table.c.credits,
        "_run": credit_entry_table.c.tailoring_run_id,
        "_at": credit_entry_table.c.created_at,
    },
)

mapper_registry.map_imperatively(
    CreditAccount,
    credit_account_table,
    properties={
        "_user_id": credit_account_table.c.user_id,
        "_opened_at": credit_account_table.c.opened_at,
        "_first_pack_click_at": credit_account_table.c.first_pack_click_at,
        "_payment_notice_opted_in_at": credit_account_table.c.payment_notice_opted_in_at,
        "_entries": relationship(
            CreditEntry,
            lazy="selectin",
            order_by=(credit_entry_table.c.created_at, credit_entry_table.c.id),
            passive_deletes=True,
        ),
    },
)
