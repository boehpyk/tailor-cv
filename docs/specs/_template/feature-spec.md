# Feature Spec: <feature name>

> The *what & why*. Disposable — this dies once the feature ships and its behaviour lives in tests +
> code. Make it **executable** (measurable, enumerated), not pretty. Must not contradict the
> [Constitution](../../constitution.md) or an accepted ADR.

- **Feature:** <name>
- **Bounded context(s):** <intake | posting | tailoring | export | identity | retention>
- **Related PRD items:** <US-x / FR-x>
- **Status:** Draft | Approved | Shipped
- **Date:** YYYY-MM-DD

## Ubiquitous language (this slice)

Define the terms this feature introduces or touches, so code, API and UI share one vocabulary.

| Term | Means | Not to be confused with |
|---|---|---|
| <Base CV> | <the user's source CV, as uploaded and extracted> | resume, document, file |

## User story

As a **<guest / registered user>**, I want **<capability>** so that **<outcome>**.

## In scope

- <bullet>

## Non-goals (explicit — hold the line)

- <what this feature deliberately does NOT do>

## Acceptance criteria (the Definition of Done checklist)

Enumerated, measurable, each independently checkable. These are what `/verify` checks off.

- [ ] AC-1: <observable behaviour, incl. the value/threshold>
- [ ] AC-2: <failure/edge behaviour>
- [ ] AC-3: <authorization / privacy rule respected — who may see this, and what is never logged>
- [ ] AC-4: <performance budget if relevant, e.g. tailoring completes within 15 s>
- [ ] AC-5: <the React surface: what the user sees while it works, and when it fails>

## Failure contract

What happens when things go wrong — enumerated here, not invented at implementation time. Every row
becomes a test.

| Condition | Expected behaviour |
|---|---|
| <invalid input> | <validation error at the boundary, no domain mutation, message the user can act on> |
| <LLM unavailable / rate-limited> | <run recorded as failed, retryable, user told what to do> |
| <LLM returns unparseable output> | <treated as a failure, not persisted as content> |
| <dependency down (Postgres/Redis/worker)> | <graceful degrade or explicit error — never a hang> |
| <guest session expired> | <clear message, path back to a fresh workspace> |

## Privacy check (Constitution §8)

- What personal data does this slice touch? <…>
- Where does it go? <…>  Where is it *never* logged? <…>
- What deletes it, and when? <…>
