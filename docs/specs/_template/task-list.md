# Task List: <feature name>

> Ordered, small tasks in canonical order. Each should be reviewable in < 5 minutes and is one commit
> on `feature/<name>`. Run `make check` before each commit. Check off as you go.

## Domain (`domain-modeler`)
- [ ] T1: <value object(s)>
- [ ] T2: <aggregate + invariants>
- [ ] T3: <domain event(s) / domain errors>
- [ ] T4: <port Protocol(s)>

## Application (`domain-modeler`)
- [ ] T5: <use case + input dataclass>

## Infrastructure (`api-dev`)
- [ ] T6: <table + imperative mapping>
- [ ] T7: <repository adapter>
- [ ] T8: <Alembic migration (hand-reviewed)>
- [ ] T9: <external adapter — LLM / extractor / fetcher / renderer / file store>
- [ ] T10: <Celery task, if the work leaves the request>
- [ ] T11: <FastAPI router + Pydantic schemas + boundary validation + rate limit>
- [ ] T12: <wiring in the composition root>

## Frontend (`react-dev`)
- [ ] T13: <typed API client function + TanStack Query hook>
- [ ] T14: <component(s) + loading / error / empty states>
- [ ] T15: <wire into the route and the workspace flow>

## Tests (`qa`)
- [ ] T16: <domain unit tests>
- [ ] T17: <application tests against the fake LlmPort + real DB>
- [ ] T18: <API tests, including every row of the failure contract>
- [ ] T19: <Vitest component tests>

## Verify
- [ ] T20: `/verify` → reviewer PASS, all acceptance criteria checked, docs updated
      (CLAUDE.md / ADR / FORboehpyk.md).
