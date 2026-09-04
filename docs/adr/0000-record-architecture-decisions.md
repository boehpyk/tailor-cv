# ADR-0000: Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-09-04

## Context

This is a solo, long-lived project with a second life as a Python/React learning vehicle. Decisions
made now — why a port in front of Gemini, why imperative SQLAlchemy mapping, why Celery for exports —
will be forgotten in three months. "Spec-in-the-drawer" is architectural, not a discipline failure:
decisions rot when they live only in chat.

For a learning project the stakes are higher than for a normal one. A decision you cannot reconstruct
is a lesson you did not learn.

## Decision

We keep **Architecture Decision Records** as short, numbered, immutable markdown files in
`docs/adr/`. Each records one significant, hard-to-reverse decision: the context, the decision, the
alternatives weighed, and the consequences. A decision is never edited to change its meaning — it is
**superseded** by a new ADR that references it.

Format per record:

```
# ADR-NNNN: Title
- Status: Proposed | Accepted | Superseded by ADR-XXXX
- Date: YYYY-MM-DD
## Context      (forces at play — technical, product, learning)
## Decision     (what we chose, stated plainly)
## Alternatives (what we rejected and why)
## Consequences (what this makes easy, what it makes hard, what we watch)
```

## Consequences

- The Constitution's stack tables link to the ADR that justifies each row.
- `/plan` and the reviewer treat accepted ADRs as binding until superseded.
- Reversing a decision is cheap to *record* and honest — you write ADR-00XX "Supersedes 000Y".
