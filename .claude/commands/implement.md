---
description: Build an approved feature in canonical order (Implement phase)
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

After each task: run `make check` and only commit when it is green. Keep commits small (reviewable in
under five minutes) with imperative messages. Check off tasks in `task-list.md` as you go.

**Do not stop at the API.** A slice is not done until its React surface exists and a user can reach
the behaviour (sdlc.md).

When the task list is complete, tell the user and suggest running `/verify $1`.
