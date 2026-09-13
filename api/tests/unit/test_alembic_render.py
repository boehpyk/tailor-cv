"""`render_item`'s three impl shapes (AC-1), pinned against Alembic's own `AutogenContext`.

Not a domain unit test (there is no I/O here to leak, but the module under test is infrastructure,
not `domain/`), placed in `unit/` regardless because it needs no database — an `AutogenContext` is
constructed in-process, entirely against Alembic's own objects, exactly as the module's docstring
measures it against Alembic 1.19.1 / SQLAlchemy 2.0.52.

Expected strings are copied from AC-1 and the module's own docstring table, not captured from a
run of the hook — a test written by recording the answer could never disagree with a regression in
the code that produced it.
"""

from __future__ import annotations

from alembic.autogenerate.api import AutogenContext
from alembic.runtime.migration import MigrationContext
from sqlalchemy import String, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import TypeDecorator

from tailorcraft.infrastructure.persistence.alembic_render import render_item
from tailorcraft.infrastructure.persistence.types.tailoring import TailoringRunStatusType


class _StringDecorator(TypeDecorator[str]):
    """A local `TypeDecorator` over `String(16)`, so this test documents the shape render_item
    promises rather than depending on any one project decorator's continued existence."""

    impl = String(16)
    cache_ok = True


class _UuidDecorator(TypeDecorator[str]):
    """A local `TypeDecorator` over `UUID(as_uuid=True)` — the one branch `render_item` renders by
    hand, because SQLAlchemy 2.0 makes `postgresql.UUID` and `sa.UUID` the same class and Alembic's
    own renderer would otherwise spell it `sa.UUID()`."""

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True


class _TimestampDecorator(TypeDecorator[str]):
    """A local `TypeDecorator` over `TIMESTAMP(timezone=True, precision=0)` — the whole-second,
    timezone-aware instant column type ADR-0007 requires everywhere in the schema."""

    impl = postgresql.TIMESTAMP(timezone=True, precision=0)
    cache_ok = True


def _autogen_context() -> AutogenContext:
    """Measured, not guessed: `MigrationContext.configure` does not set the module-prefix defaults
    Alembic's CLI normally supplies, so `AutogenContext` would otherwise render types with no `sa.`
    / `postgresql.` prefix at all. Constructed fresh per call so each test's `imports` set starts
    empty."""
    migration_context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={
            "render_item": render_item,
            "sqlalchemy_module_prefix": "sa.",
            "alembic_module_prefix": "op.",
            "user_module_prefix": None,
        },
    )
    return AutogenContext(migration_context)


def test_a_string_backed_decorator_renders_as_sa_string_with_no_import_registered() -> None:
    """`import sqlalchemy as sa` is never added to `autogen_context.imports`: Alembic's own
    renderer never registers it either, because `script.py.mako` imports it unconditionally, and a
    second copy emitted through `${imports}` would fail Ruff (F811) in every generated draft."""
    autogen_context = _autogen_context()

    rendered = render_item("type", _StringDecorator(), autogen_context)

    assert rendered == "sa.String(length=16)"
    assert autogen_context.imports == set()


def test_a_uuid_backed_decorator_renders_as_postgresql_uuid_with_the_dialect_import() -> None:
    autogen_context = _autogen_context()

    rendered = render_item("type", _UuidDecorator(), autogen_context)

    assert rendered == "postgresql.UUID(as_uuid=True)"
    assert autogen_context.imports == {"from sqlalchemy.dialects import postgresql"}


def test_a_timestamp_backed_decorator_renders_as_postgresql_timestamp_with_the_dialect_import() -> (
    None
):
    autogen_context = _autogen_context()

    rendered = render_item("type", _TimestampDecorator(), autogen_context)

    assert rendered == "postgresql.TIMESTAMP(timezone=True, precision=0)"
    assert autogen_context.imports == {"from sqlalchemy.dialects import postgresql"}


def test_a_real_project_decorator_renders_the_same_way_as_its_impl() -> None:
    """Ties this test to the schema it actually protects: `TailoringRunStatusType` is
    `tailoring_run.status`, `VARCHAR(16)`, and this is the exact rendering a migration draft needs
    to be importable (AC-1's "the fifth draft is the first that loads")."""
    autogen_context = _autogen_context()

    rendered = render_item("type", TailoringRunStatusType(), autogen_context)

    assert rendered == "sa.String(length=16)"
    assert autogen_context.imports == set()


def test_a_non_decorator_type_is_left_to_alembics_own_renderer() -> None:
    """Alembic's `render_item` contract: `False` is the "render it yourself" sentinel — not
    `None`, which Alembic would take as an already-rendered (empty) string."""
    autogen_context = _autogen_context()

    assert render_item("type", Text(), autogen_context) is False


def test_a_non_type_item_is_left_to_alembic_regardless_of_the_object() -> None:
    """`render_item` is also called for `"server_default"`, `"column"` and other item kinds; the
    hook only ever claims `"type"`."""
    autogen_context = _autogen_context()

    assert render_item("server_default", _StringDecorator(), autogen_context) is False
