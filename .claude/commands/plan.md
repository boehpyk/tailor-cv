---
description: Write and get approval for a feature spec (Plan phase)
argument-hint: <feature-name>
---

Plan the feature `$1` using the **planner** subagent.

1. If `docs/specs/$1/` does not exist yet, run `/new-feature $1` first.
2. Delegate to the `planner` agent: have it read `docs/constitution.md`, the ADRs in `docs/adr/`,
   `docs/PRD.md` and `docs/roadmap.md`, then fill in the three files under `docs/specs/$1/`:
   `feature-spec.md`, `technical-plan.md`, `task-list.md`.
3. The plan must name the bounded context(s), the aggregate(s) and their invariants, the value
   objects, domain events and ports, **the API contract**, and **the React surface** — and must not
   contradict the Constitution or any accepted ADR. Flag any decision that warrants a new ADR (`/adr`).
4. Check the plan carries the two things that are easiest to skip and most expensive to omit here:
   an enumerated **failure contract** (LLM timeout / rate limit / unparseable output / unfetchable
   URL / worker down), and the **privacy position** (what PII this touches, what is never logged,
   what deletes it).

**Then STOP and present the plan to the user for approval.** Do not implement anything until the user
approves. This human gate is the point of the Plan phase.
