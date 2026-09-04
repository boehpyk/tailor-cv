# Technical Plan: <feature name>

> The *how*. Disposable. Written after the feature-spec is drafted, approved with it before any code.
> Follows the canonical build order: domain → application → infrastructure → API → frontend → tests.

## Domain layer (pure Python — stdlib only)

- **Aggregate / entity changes:** <Aggregate, its invariants, what protects them>
- **Value objects:** <new/changed VOs and why they are VOs (immutability, equality, validation).
  Frozen dataclasses validating in `__post_init__` — never Pydantic (ADR-0002)>
- **Domain events:** <past-tense facts recorded on the aggregate; who reacts>
- **Ports (Protocols):** <RepositoryPort / LlmPort / FileStorePort / TaskQueuePort … defined here,
  speaking the domain's language, not a vendor's (ADR-0004)>
- **Domain errors:** <the exception types the application layer will translate>

## Application layer

- **Use case:** <name + input dataclass (the input contract)>
- **Flow:** <load aggregate via port → call aggregate methods → persist via port → release events>
- **Transaction boundary:** <where the unit of work opens and commits>
- **Idempotency:** <if this can be retried — and any queued work can — how duplicates are prevented>

## Infrastructure layer

- **Persistence:** <imperative mapping module, Table definition, repository adapter, Alembic migration>
- **Adapters:** <Gemini / extractor / fetcher / renderer / file store — and their failure translation>
- **Async:** <Celery task (thin entry point only), queue, retry policy, `ExportJob`-style state row>
- **Wiring:** <port → adapter in the composition root. A port with no binding is a bug.>

## API contract

The exact surface the frontend sees. Write it here so the implementation does not invent it.

| Method | Path | Request | Response | Errors |
|---|---|---|---|---|
| POST | `/api/...` | <Pydantic schema> | <Pydantic schema> | <status → meaning> |

- **Auth:** <guest session / authenticated / either — and what authorizes access to *this* object>
- **Validation at the boundary:** <file type + size sniffing, URL SSRF guard, length limits>
- **Rate limiting:** <if it costs money or CPU, it is limited — by what key, at what rate>

## Frontend (`web/`)

- **Route / screen:** <where this lives>
- **Components:** <new components, and which are presentational vs container>
- **State:** <server state → TanStack Query key and invalidation; form state → local. Justify
  anything that is neither.>
- **Custom hooks:** <the hook boundary — what it encapsulates and why it is a hook, not a component>
- **Loading / error / empty states:** <all three, explicitly — especially for the long LLM wait>

## Data & migrations

- <new tables/columns, indexes (name the index and the query it serves), expand→migrate→contract plan>

## Test plan

- **Domain unit:** <invariants, VO validation, events — no I/O, no fixtures, no event loop>
- **Application:** <use case against a fake LlmPort and a real database>
- **API:** <httpx AsyncClient against the app, real DB, transactional rollback>
- **Frontend:** <Vitest + RTL: renders, the loading state, the failure state>
- **Never:** <a test that calls the real Gemini API — ADR-0004>

## Risks / open questions

- <anything the reviewer or human should weigh in on — especially anything that would become an ADR>
