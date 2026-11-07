"""The `demand_daily_counter` table: one count per UTC day and counter (ADR-0043, plan §0.9).

A plain `Table` with no class: the counters are Core only (one upsert, one range `SELECT`). A row holds a
date, a counter name and a number, so no personal data, and it survives the purge and erasure.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, Column, Date, String, Table

from tailorcraft.domain.shared.counters import DemandCounter
from tailorcraft.infrastructure.persistence.registry import metadata

_KNOWN = ", ".join(f"'{counter.value}'" for counter in DemandCounter)

daily_counter_table = Table(
    "demand_daily_counter",
    metadata,
    Column("day", Date, primary_key=True),
    Column("counter", String(32), primary_key=True),
    Column("count", BigInteger, nullable=False),
    CheckConstraint(f"counter IN ({_KNOWN})", name="known"),
    CheckConstraint("count >= 0", name="non_negative"),
)
