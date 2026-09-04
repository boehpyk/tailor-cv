---
name: hex-slice
description: The canonical recipe for adding a vertical slice to TailorCraft — choosing the bounded context, defining value objects, the aggregate, domain events, a Protocol port, an application use case, the SQLAlchemy imperative mapping, the Alembic migration, the FastAPI router and the DI wiring. Use when building or reviewing any feature that touches the backend domain.
---

# Adding a vertical slice to TailorCraft

Follow this order every time. It keeps `domain/` pure (import-linter clean) and makes the design
legible to the person reading it in three months, who is you.

## 1. Choose the bounded context
`intake` · `posting` · `tailoring` · `export` · `identity` · `retention` (Constitution §4). Code goes
under `domain/<context>/`, `application/<context>/`, `infrastructure/<context>/`. Use the spec's
ubiquitous language exactly — a base CV is never a "resume" in code.

## 2. Domain layer (`domain/<context>/`) — stdlib only

**Value objects first** (`value_objects.py`). Frozen, validated, compared by value:

```python
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class ExtractedText:
    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise EmptyExtraction("extraction produced no text")
```

Reach for one whenever a `str` has rules. A primitive with its rules enforced somewhere else is a bug
waiting for a second caller.

**Aggregate** (`<aggregate>.py`). Invariants live here. Mutation through named methods that say what
happened in business terms:

```python
class BaseCv:
    def mark_extracted(self, text: ExtractedText, at: datetime) -> None:
        if self._extracted_at is not None:
            raise AlreadyExtracted(self.id)
        self._text = text
        self._extracted_at = at
        self.record(BaseCvExtracted(self.id, at))
```

No public setters. No `@property` setter that skips the rule. Keep the aggregate small — it is the
consistency boundary, not a bag of related data.

**Domain events** (`events.py`): past-tense facts, frozen dataclasses, carrying **value objects and
ids only**. Never the aggregate, never a secret, **never a CV body** — an event payload ends up in
every listener and every log line.

**Ports** (`ports.py`): `typing.Protocol`s, in the domain's language.

```python
class LlmPort(Protocol):
    async def tailor(self, cv: ExtractedText, posting: JobPostingText) -> TailoredPair: ...
```

Note what is *not* there: no Gemini, no model name, no retry count, no JSON. Those are the adapter's
business (ADR-0004).

**Domain errors** (`errors.py`): explicit types the API layer translates to status codes. The domain
never raises `HTTPException`.

## 3. Application layer (`application/<context>/`)

One use case per user intent, with a frozen input dataclass as its contract:

```python
class ExtractBaseCv:
    def __init__(self, cvs: BaseCvRepository, extractor: CvTextExtractorPort, clock: Clock) -> None:
        self._cvs, self._extractor, self._clock = cvs, extractor, clock

    async def __call__(self, cmd: ExtractBaseCvCommand) -> BaseCvId:
        cv = await self._cvs.get(cmd.cv_id)
        cv.mark_extracted(await self._extractor.extract(cv.file), self._clock.now())
        await self._cvs.save(cv)
        await self._events.publish(*cv.release_events())
        return cv.id
```

Dependencies arrive as **ports through the constructor**. The handler orchestrates; it does not
decide. If a rule is being expressed here, it belongs in the aggregate.

## 4. Infrastructure layer (`infrastructure/`)

1. **Table + imperative mapping** (`persistence/mapping/<context>/<aggregate>.py`) — a `Table` and
   `registry.map_imperatively(BaseCv, base_cv_table)`. **Never `class BaseCv(Base)`, never
   `mapped_column` on a domain class** (ADR-0007). Value objects map through a `TypeDecorator`.
2. **Repository adapter** implementing the port. It hides the `Session`; no session crosses into
   `application/`.
3. **Migration:** `make migration.make name="..."` → **read every line** → additive and
   backward-compatible.
4. **External adapter** if the slice has one: translate vendor errors into domain errors, apply the
   timeout and bounded retry, record the duration. Never log the payload.
5. **Router** (`api/routers/<context>.py`) with Pydantic schemas. **This is the validation boundary**
   — sniff file content, cap sizes, guard URLs, bound lengths. Translate domain errors to statuses.
6. **Wiring:** bind the port to the adapter in the composition root. A port with no binding is a bug.

## 5. Prove it
`qa` writes domain unit tests (invariants, VO validation, events — no I/O at all) plus application and
API tests against the real test database and the **fake `LlmPort`**. Run `make check`. `lint-imports`
showing zero violations is the proof the domain stayed pure.

## Common mistakes
- **Anemic aggregate** — rules in the use case instead of the entity. Move them into the aggregate.
- **Pydantic in `domain/`** — "just for validation". It is a third-party import and it drags
  serialization concerns into business rules. Frozen dataclass instead.
- **Declarative SQLAlchemy on a domain class** — the tutorial path, and the one that ends the design.
- **A use case importing an adapter** — it becomes untestable and unswappable in the same line.
- **A blocking call in an async path** — presents as "slow under load", never as an error.
- **A port that speaks the vendor's language** — if `LlmPort` mentions Gemini, it is not a port, it is
  a rename.
