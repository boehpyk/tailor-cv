"""The mapper registry — where plain domain classes are married to database tables.

This is the file that makes ADR-0007 work, and it is worth understanding rather than copying.

Every SQLAlchemy tutorial shows **declarative** mapping: `class BaseCv(Base)` with `mapped_column()`
on each attribute. It is clean, it is popular, and it puts an ORM import inside the class that
models your business. The domain layer would be over before it began.

So this codebase uses **imperative** mapping instead. A domain class stays plain Python, defined in
`tailorcraft.domain` with no idea that persistence exists. Separately, a module under `mapping/`
declares a `Table` and calls `mapper_registry.map_imperatively(BaseCv, base_cv_table)`. SQLAlchemy
wires the two together at import time.

The cost is that the class and the table are two shapes kept in step by hand. The compensation is
that a mismatch raises a mapper configuration error at **startup** — loud, immediate, and impossible
to deploy past.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import registry

# Explicit naming conventions. Without them, PostgreSQL invents constraint names, Alembic
# autogenerate cannot match a constraint it did not name, and every subsequent migration proposes
# dropping and recreating things that never changed.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)
mapper_registry = registry(metadata=metadata)


def configure_mappings() -> None:
    """Import every mapping module so its `map_imperatively()` call runs.

    Imperative mapping has one failure mode that declarative does not: a mapping module nobody
    imports simply never runs, and the aggregate is silently unmapped until the first query fails
    with a confusing error. Calling this once at startup — and once in the Alembic environment, and
    once in the test session — is what makes that impossible.
    """
    from tailorcraft.infrastructure.persistence import mapping

    mapping.load_all()
