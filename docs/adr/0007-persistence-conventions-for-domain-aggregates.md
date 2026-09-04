# ADR-0007: Persistence conventions — SQLAlchemy imperative mapping, Alembic, app-assigned UUIDv7

- **Status:** Accepted
- **Date:** 2026-09-04

## Context

Constitution §4 requires a `domain` layer that imports nothing third-party. SQLAlchemy's declarative
style — the one in every tutorial — puts the ORM *inside* the model class: `class BaseCv(Base)`,
`Mapped[str]`, `mapped_column(...)`. That is a third-party import in the domain and the design is over
before it starts.

This is the single most likely place for the architecture to quietly collapse, because the declarative
style is the well-known one and the alternative is barely mentioned in the docs.

## Decision

**Imperative (classical) mapping.** Domain classes are plain Python. A separate module in
`infrastructure/persistence/mapping/` declares a `Table` and calls
`registry.map_imperatively(BaseCv, base_cv_table)`. The domain class never learns it is persisted.

Conventions, established by the first slice and inherited by every context:

- **One mapping module per aggregate**, at
  `infrastructure/persistence/mapping/<context>/<aggregate>.py`. **Never a declarative base or a
  `mapped_column` on a domain class** — that is a third-party import in `domain/` and `lint-imports`
  fails the build.
- **Value objects map through custom `TypeDecorator`s**, in
  `infrastructure/persistence/types/`, not through composites. A `FileRef` or an `EmailAddress` is one
  column with a type that knows how to round-trip it.
- **Tables are `<context>_<aggregate>`, singular** (`intake_base_cv`), with every column named
  explicitly rather than derived by a naming convention. A schema you can read without knowing the
  mapper's rules is worth the extra typing.
- **Column types are chosen, not inherited from defaults.** JSON is `JSONB`. Timestamps are
  `TIMESTAMP WITH TIME ZONE` and **whole-second**: the `Clock` port returns whole seconds and the
  system implementation truncates at the source, so a round trip through the database can never
  change a value. Any hand-written test `Clock` must honour that too, or an equality assertion will
  fail by microseconds on a day you have no time for it.
- **Identity is application-assigned UUIDv7**, minted by the repository's `next_identity()`. The
  aggregate is fully valid before it ever meets the database, which is what makes domain tests
  possible without one. UUIDv7 rather than v4 because it is time-ordered and therefore index-friendly.
- **Repositories implement the domain port and hide the session.** No `Session` object crosses into
  `application/`; the unit of work is opened at the boundary (a request or a task) and committed
  there.
- **Alembic migrations are hand-reviewed, always.** Autogenerate against imperatively-mapped tables is
  a *draft*. Migrations are additive and backward-compatible (expand → migrate → contract) because the
  deploy briefly runs two versions at once.

## Alternatives

- **Declarative mapping on domain classes.** The popular path. Rejected: it violates Constitution §4
  directly, and every later attempt to unpick it is a rewrite of the model layer.
- **Separate ORM entities plus hand-written mappers to domain objects.** Honest and explicit, and
  roughly twice the code — you write the translation that `map_imperatively` writes for you, and you
  give up the unit of work's change tracking on domain objects. Rejected as the default; it stays the
  fallback if imperative mapping proves too limiting for a specific aggregate.
- **A document store / JSONB blob per aggregate.** Rejected: application history and (Phase 3)
  tracking want real queries and real indexes.
- **Database-generated integer ids.** Rejected: the aggregate would be invalid until saved, which
  forces the database into tests that have no other reason to need it.

## Consequences

- Two shapes per aggregate — the domain class and the `Table` — kept in step by hand. A mismatch shows
  up as a mapper configuration error at startup, which is loud and immediate; that loudness is the
  reason this is tolerable.
- `mypy --strict` sees plain classes rather than SQLAlchemy's `Mapped[...]` machinery, which is a real
  ergonomic win in the layer that matters most.
- **`make:migration`-style autogenerate will try to drop things it cannot see.** Review every
  generated migration line by line. An index created by hand and not expressed in the `Table` will be
  proposed for deletion on every subsequent autogenerate, forever.
- The domain stays testable with zero I/O, which is the whole point and the thing to protect when a
  shortcut is tempting.
