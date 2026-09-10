# ADR-0002: Hexagonal layers, enforced by import-linter

- **Status:** Accepted
- **Date:** 2026-09-04

## Context

FR-7 makes clean separation of concerns a *requirement*, not a preference: the codebase is a
pedagogical reference. But layering that is only written down decays within a fortnight — the first
time a use case needs "just one" `select()` and the ORM is already imported two files away.

Python makes this harder than a compiled language. Nothing stops `domain/cv.py` from importing
SQLAlchemy; the code runs fine and the design is dead. Reviews catch it inconsistently, because the
violation is one plausible-looking line.

## Decision

Three layers under `api/src/tailorcraft/`, with strictly ordered dependencies:

| Layer | May import | Must NOT import |
|---|---|---|
| `domain` | the standard library only | any third-party package, `application`, `infrastructure` |
| `application` | `domain` + stdlib | `infrastructure`, SQLAlchemy, FastAPI, any adapter |
| `infrastructure` | `domain` + `application` + anything installed | — |

Enforced by **two mechanisms**, because neither one alone does the whole job:

1. **import-linter** (`lint-imports`), run in `make check` and in CI, with three contracts: a
   `layers` contract (`infrastructure` → `application` → `domain`) that catches an upward import,
   and two `forbidden` contracts naming the frameworks and vendors that must not reach `domain` or
   `application`.
2. **`tests/unit/test_domain_purity.py`**, which parses every module under `domain/` and rejects any
   import that is not the standard library or `tailorcraft` itself.

The second exists because the first is an **allow-list by omission**: import-linter's `forbidden`
contracts only know about packages someone thought to name, so a dependency added six months from
now by someone who never opened `pyproject.toml` sails straight past them. The AST test is
**default-deny** and needs no maintenance at all.

*(An earlier draft of this ADR claimed the forbidden contract could be expressed as a wildcard over
every installed package. It cannot, in any way that is verifiable — which is why the default-deny
half is a test rather than a contract. Recorded rather than quietly edited: the reasoning that a
boundary must fail closed was right; the mechanism named for it was wrong.)*

Both were verified by regression rather than assumed: injecting `import httpx` into a domain module
turns the contract **BROKEN** and the test red, and removing it makes both green again.

Additionally: **Pydantic is a third party and stays out of `domain`.** Domain value objects are
frozen dataclasses that validate in `__post_init__`. Pydantic models are the HTTP wire format and
live in `infrastructure/api/schemas/`.

## Alternatives

- **Convention plus code review.** How it usually goes, and how it usually ends. Rejected: the whole
  value of the boundary is that it holds when you are tired.
- **A single flat package with "we'll be careful".** Faster for a week. Rejected — see FR-7.
- **Pydantic models as the domain model.** Tempting: validation, immutability with
  `model_config = ConfigDict(frozen=True)`, less code. Rejected because the domain would then inherit
  serialization concerns, JSON aliases, and a validation error type that belongs to the HTTP layer —
  and because it makes the domain depend on a library whose major-version upgrades have historically
  been rewrites. The value object is where business meaning lives; it should not also be the wire
  format.
- **`pytest-archon` / a custom AST test.** Works, but re-implements a solved tool.

## Consequences

- **There will be mapping code**, and that is the price, paid on purpose. A domain `BaseCv` is not the
  SQLAlchemy row and not the Pydantic response — three shapes, two translations. It feels like
  friction in week one and is the reason the design survives to month six.
- The `domain` layer is testable with no fixtures, no database, no event loop and no network. Domain
  tests should be the fastest and most numerous tests in the suite; if they are not, logic has leaked
  outward.
- A port that is defined but never bound is a bug. Wiring lives in **one** composition root so that
  "is everything bound?" is a question with one place to look.
- `lint-imports` must run on the *installed* package, so the `make imports` target and the CI step run
  from the same working directory and the same virtualenv. A contract that silently matches nothing
  passes.
