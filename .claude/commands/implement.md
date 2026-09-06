---
description: Build an approved feature in canonical order, test-first where it pays (Implement phase)
argument-hint: <feature-name>
---

Implement the **approved** plan for `$1` by working `docs/specs/$1/task-list.md` in order.

Only proceed if the plan has been approved by the user. Work tasks in canonical order, delegating to
the right specialist and committing per task on the `feature/$1` branch:

1. **Domain + application tasks** → `domain-modeler` agent (value objects, aggregates, events, ports,
   use cases; pure Python, stdlib only in `domain/`).
2. **Infrastructure tasks** → `api-dev` agent (SQLAlchemy imperative mapping, repositories, Alembic
   migration, adapters, Celery tasks, FastAPI routers + Pydantic schemas, wiring).
3. **Frontend tasks** → `react-dev` agent (typed API client, TanStack Query hooks, components, the
   three states).
4. **Test tasks** → `qa` agent (domain unit tests, application/API tests against the real test DB and
   the fake `LlmPort`, Vitest component tests).

## Tiered TDD — the red-first cycle (sdlc.md §2)

These layers are **red-first**, and the order is not negotiable:

- domain value objects, aggregates, events
- application use cases
- **every row of the spec's failure contract**
- the FastAPI router + schemas (the HTTP contract)
- the React loading / error / empty / success states

For each, run three tasks and three commits:

1. **SKELETON** — the *owning implementer* (`domain-modeler` / `api-dev` / `react-dev`) writes real
   signatures with `NotImplementedError` bodies. No behaviour. This exists so the test can fail on
   its assertion instead of on an import.
2. **RED** — `qa` writes the test and **runs it**. Paste the actual failing output into the commit
   body as `Recorded red: <assertion>` and into the task-list line.
3. **GREEN** — the same implementer makes it pass without touching the test.

**A red that is an `ImportError` does not count.** It proves a file is absent, not that the assertion
discriminates. If you see one, the skeleton was skipped or is incomplete — go back to step 1.

**`qa` never writes production code**, skeletons included. If a red test cannot run because something
is missing, hand it back to the owning implementer; do not have `qa` stub it.

These layers stay **test-after** — do not force a red cycle on them: SQLAlchemy mappings, repository
adapters, Alembic migrations, external adapters, DI wiring, and component structure/markup. Their
shape is discovered against the library, so test-first there buys rewrite churn, not confidence.

## Per task

After each task: run `make check` and only commit when it is green.

**The one exception is a RED task**, whose new test is *supposed* to fail. Commit it with:

```bash
make check.static                       # every gate except pytest and vitest
TDD_RED=1 git commit -m "..."           # the pre-commit hook swaps in check.static
```

`TDD_RED=1` only opens when a test file is actually staged — it is not a lever for getting past a
suite you have not fixed. Never use `--no-verify`: that would also skip the secret, PII and published
-port guards, which have nothing to do with the test being red. The full gate is restored by the
GREEN commit that follows, and a RED commit must never be the last commit on the branch.

Keep commits small (reviewable in under five minutes) with imperative messages. Check off tasks in
`task-list.md` as you go.

**Do not stop at the API.** A slice is not done until its React surface exists and a user can reach
the behaviour (sdlc.md).

When the task list is complete, confirm every RED task carries a recorded failure, tell the user, and
suggest running `/verify $1`.
