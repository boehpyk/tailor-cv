"""The `retention` bounded context: guest-owned data lives at most one window, and something enforces that.

**There is no aggregate here, and that is a decision rather than an omission** (ADR-0018 decision 1,
and Constitution §4's table before it). What this context owns is a *policy* — two use cases, two
ports, a handful of value objects and its errors. An aggregate exists to protect an invariant across
a transaction boundary, and a purge run has none; the one invariant the purge leans on (`is_expired`)
already belongs to `GuestSession` in `identity`, where ADR-0006 §1 put it. `retention` **applies**
that predicate, it does not restate it. ADR-0018 decision 9 adds the rest of the negative space: no
migration, no new queue, and **no domain event** — a `GuestDataPurged` payload would reach every
listener and every log line, which is the one place Constitution §8 says a CV-adjacent value must
never go.

**What this package may import is a short list, and AC-1 makes a test of it:** the standard library,
`domain.shared` (`Clock`, `FileRef`, `FileStorePort`, `DomainError`) and exactly one name out of
`domain.identity` (`GuestSessionId`). Never `domain.intake`, `domain.posting`, `domain.tailoring` or
`domain.export` — their rows go by the **database cascade** and their files by a **derived key**, so
an import of one of them would not be an import, it would be a design change: it would mean the
cascade contract or `FileRef`'s determinism guarantee had stopped holding. (`domain.shared.files`
imports `intake` and `export` itself so that `FileRef` can derive keys from their ids; that is
`files.py`'s business, argued in its own docstring, and depending on `shared` is not the same as
depending on them.)

**This package `__init__` re-exports nothing** — the same rule, for the same reason, as
`domain/intake/__init__.py` and `domain/export/__init__.py`; `domain/shared/files.py`'s module
docstring is where the argument is written down for all of them.
"""
