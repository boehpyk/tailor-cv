"""Alembic `render_item` hook: a `TypeDecorator` column renders as its `impl`.

**Why this exists.** Every migration draft of slices 1.1 to 1.3 rendered each value-object column as
the decorator that maps it — `sa.Column("status", TailoringRunStatusType(), nullable=False)` — and
a migration module cannot import that name: `alembic/versions/` is not a package and does not import
the ORM layer. The draft `NameError`ed at load, and each revision was fixed by hand, four times over,
column by column (the "`TailoringRunIdType.impl` — a native Postgres UUID" comments in the existing
revisions are the scar tissue). This hook makes the fifth draft the first one that loads.

**Why it is also the structurally right rendering.** A migration describes the schema; the
decorator is the ORM layer that happens to produce that schema today. If `TailoringRunStatusType`
is renamed, split or deleted next year, the column is still `VARCHAR(16)`, and a revision that named
the decorator would be lying about what it created. Rendering the `impl` — the type the decorator
delegates to for DDL — is the truthful spelling, and it is what the hand-fixed revisions already say.

**Why the file is here and not next to `env.py`.** `env.py` runs the migration at import time, so a
unit test cannot import it. `alembic/` is not a package either (no `__init__.py`, and `alembic` is
also the library's own name), so the hook lives in the installed package and `env.py` imports it.

**How it works.** Alembic calls `render_item("type", <TypeEngine>, autogen_context)` before rendering
any column type and takes a returned string verbatim; `False` means "render it yourself". For a
`TypeDecorator` we hand the *impl* back to Alembic's own type renderer, which already knows the
`sa.` prefix, the per-dialect import (`from sqlalchemy.dialects import postgresql`) and `Variant`s —
and that renderer calls this hook again on the impl, which is not a decorator, so it returns
`False` and the recursion ends there. The one shape rendered by hand is `UUID`, for the reason on
the branch below. Measured against Alembic 1.19.1 / SQLAlchemy 2.0.52, and pinned by the P1t unit
test:

    String(16)                              -> "sa.String(length=16)"
    Text                                    -> "sa.Text()"
    UUID(as_uuid=True)                      -> "postgresql.UUID(as_uuid=True)"
    TIMESTAMP(timezone=True, precision=0)   -> "postgresql.TIMESTAMP(timezone=True, precision=0)"

`import sqlalchemy as sa` is deliberately **not** added to `autogen_context.imports`: Alembic's own
renderer never registers it because `script.py.mako` imports it unconditionally, and a second copy
emitted through `${imports}` would fail Ruff (F811) in every generated draft.

**The one thing to re-measure on an Alembic bump:** the impl is handed to
`alembic.autogenerate.render._repr_type`, a *private* function with no compatibility promise, so a
new Alembic version can rename it, change its signature or change what it emits without a release
note — re-run the P1t unit test above before trusting the first draft after the bump.
"""

from __future__ import annotations

from typing import Literal

from alembic.autogenerate.api import AutogenContext
from alembic.autogenerate.render import _repr_type
from sqlalchemy.sql import sqltypes
from sqlalchemy.types import TypeDecorator

POSTGRESQL_IMPORT = "from sqlalchemy.dialects import postgresql"


def render_item(type_: str, obj: object, autogen_context: AutogenContext) -> str | Literal[False]:
    """Render a `TypeDecorator` as its `impl`; leave everything else to Alembic.

    Alembic's contract for the hook: `type_` names what is being rendered (`"type"`, `"server_default"`,
    `"column"`, …), `obj` is the thing itself, and `False` is the "not mine" sentinel — not `None`,
    which Alembic would take as a rendered string.
    """
    if type_ != "type" or not isinstance(obj, TypeDecorator):
        return False

    # `TypeDecorator.__init__` runs `to_instance` over the class attribute, so at runtime `obj.impl`
    # is an instance even when the decorator wrote `impl = Text` (the class) rather than
    # `impl = Text()`. The *annotation* still says instance-or-class, though, which is why this
    # reads the `impl_instance` property SQLAlchemy provides for exactly this purpose.
    impl = obj.impl_instance

    if isinstance(impl, sqltypes.UUID):
        # Since SQLAlchemy 2.0 `postgresql.UUID` *is* `sqlalchemy.UUID` — one class, re-exported —
        # so Alembic's renderer spells it `sa.UUID()`: correct, importable and the same DDL. It is
        # also a different spelling from the four hand-fixed revisions, which all say
        # `postgresql.UUID(as_uuid=True)`, and a reader diffing the fifth revision against the fourth
        # should not find every id column re-spelled for no schema reason. `as_uuid` is written out
        # rather than left to its default because the typed-id decorators depend on it being
        # `True` (they wrap a `uuid.UUID`, never a `str`), and a migration that says so is honest.
        autogen_context.imports.add(POSTGRESQL_IMPORT)
        return f"postgresql.UUID(as_uuid={impl.as_uuid!r})"

    # Everything else is Alembic's job. `_repr_type` is the function Alembic itself calls for every
    # column type; going through it rather than `repr(impl)` is what gets the `sa.` prefix and the
    # `postgresql` import registered for `TIMESTAMP(timezone=True, precision=0)`.
    return _repr_type(impl, autogen_context)
