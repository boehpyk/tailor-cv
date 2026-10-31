"""The `scoring_daily_spend` table: one row per UTC day, in whole micro-USD (ADR-0041 decision 1).

A plain `Table` with no class, since the ledger is Core only. Holds a date and a number, so no personal
data, and it survives every deletion, which is what makes the cap survive the purge.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, Column, Date, Table
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tailorcraft.infrastructure.persistence.registry import metadata

daily_spend_table = Table(
    "scoring_daily_spend",
    metadata,
    Column("day", Date, primary_key=True),
    Column("spent_microusd", BigInteger, nullable=False),
    Column("updated_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    CheckConstraint("spent_microusd >= 0", name="spent_not_negative"),
)
