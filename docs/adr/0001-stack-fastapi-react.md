# ADR-0001: FastAPI backend, React + Vite frontend

- **Status:** Accepted
- **Date:** 2026-09-04

## Context

PRD §9 names the stack in outline: FastAPI with Pydantic schemas, an ORM, Celery for background jobs;
Vite + React with custom hooks and Tailwind. This ADR pins it and says *why*, because "the PRD said
so" is not a reason you can reason from later.

Three forces:

1. **Product.** The core flow is one long-running, I/O-bound call to an LLM, plus a few heavy CPU
   renders. Async I/O for the first, a worker queue for the second.
2. **Learning (ranked — Constitution §2).** The owner wants idiomatic Python and modern React. The
   stack must be one where "idiomatic" is well defined and where the good patterns are the popular
   ones, so that reading the ecosystem teaches rather than misleads.
3. **Solo operation.** Two languages is already the maximum. Nothing here may require a third runtime
   or a build step nobody remembers.

## Decision

**Backend: Python 3.13 + FastAPI, async routes, Pydantic v2 at the boundary, `uv` for dependencies.**

FastAPI's dependency-injection system is the natural composition root for a ports-and-adapters design
— a route declares the port it needs and the wiring hands it an adapter. Pydantic gives request and
response validation at the edge for free, and its docs-generation means the React client always has a
current, typed contract to generate against.

`uv` over Poetry/pip-tools: it is fast enough that a clean install in CI is not a cache problem, it
resolves and locks in one tool, and `uv.lock` is committed so dev, CI and the image install the exact
same bytes.

**Frontend: React 19 + TypeScript (strict) + Vite + Tailwind v4 + TanStack Query.**

Vite because the dev-server feedback loop is the thing a learner interacts with most. TypeScript
strict because the whole point of the exercise is to learn the patterns that scale, and untyped React
teaches the wrong ones. TanStack Query because the single most common React mistake is hand-rolling
server-state caching in `useEffect`, and the best way to not learn that habit is to have a correct
tool from day one.

## Alternatives

- **Django + DRF.** More batteries, and a much heavier framework to hold at arm's length from a pure
  domain layer — Django's ORM is not designed to be kept out of the model. Rejected: it would fight
  Constitution §4 daily, and the fight teaches the wrong lesson.
- **Flask.** Lighter, but no first-class async, no built-in validation, no schema generation. We would
  rebuild a third of FastAPI by hand.
- **Next.js / full-stack TypeScript.** One language, genuinely attractive. Rejected because the
  owner's ranked goal is *Python and React*, and a Node backend deletes half of it.
- **Redux / Zustand as the primary state layer.** Rejected as the default: most state in this app is
  server state, and putting server state in a global store is exactly the anti-pattern to avoid. A
  small client store is allowed later if something genuinely global appears.

## Consequences

- Async all the way through the API — so **every blocking call is a bug**. `pypdf`, `python-docx` and
  WeasyPrint are synchronous and CPU-bound; they run in the worker or in a thread pool, never inline
  in an event loop. This is the first thing to get wrong and the easiest to miss, because it presents
  as "the app is slow under two users", not as an error.
- Two lint/type/test toolchains to keep in lockstep. `make check` runs both; CI runs both as separate
  jobs so the failures are separable.
- Pydantic is available everywhere and must be kept **out of `domain/`** (Constitution §4.1,
  ADR-0002). This is the most likely accidental violation in the codebase, which is why it is
  machine-enforced rather than remembered.
