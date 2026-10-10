"""The foreign-database guard shared by the operator commands that write (slice 2.2's AC-31,
lifted here in slice 4.1, OQ-18): `erase-account`, `grant-role` and `revoke-role`.

Before anything is read, `SELECT current_database()` must equal the database named in
`Settings.database_url`. A connection string that routes somewhere else — a pooler default, a
service file, a hand-edited URL — is a refusal, not a surprise. The two names it carries are
database names, never credentials, so a caller may print them.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.infrastructure.settings import Settings


class ForeignDatabase(Exception):
    """The connection landed in a database other than the one `Settings.database_url` names."""

    def __init__(self, expected: str | None, actual: str) -> None:
        super().__init__("connected to an unexpected database")
        self.expected = expected
        self.actual = actual


async def refuse_a_foreign_database(session: AsyncSession, settings: Settings) -> None:
    """The database this session is connected to must be the one `DATABASE_URL` names, asked of
    the server rather than assumed from the string."""
    expected = make_url(settings.database_url).database
    actual = (await session.execute(text("SELECT current_database()"))).scalar_one()
    if actual != expected:
        raise ForeignDatabase(expected, str(actual))
