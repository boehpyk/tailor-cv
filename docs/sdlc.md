# TailorCraft SDLC — Lean Solo Spec-Driven Development

How one person ships features here without either (a) drowning in ceremony or (b) losing intent to
disposable chat. The discipline is Spec-Driven Development; the *weight* is deliberately light.

## Why this shape

The SDD literature offers an adoption test: *regulated / multi-person / brownfield / audited — three+
yes to go heavy, else stay light.* TailorCraft is solo, greenfield, unregulated → **stay light.** We
keep the parts of SDD that pay for themselves for a solo dev (a Constitution, a short executable spec
per feature, a Plan→Implement→Verify loop with real gates, a read-only reviewer) and skip the
industrial apparatus (16-agent pipelines, phase state machines, parallel worktrees).

There is a second reason to keep specs here, specific to this project: **learning Python and React is
a ranked goal** (Constitution §2). A spec written before the code is where you notice you were about
to reach for a `dict` instead of a value object, or a `useEffect` instead of a query. That noticing is
worth more than the document.

Two document lifecycles, and it matters which is which:

- **Durable:** the [Constitution](./constitution.md) and [ADRs](./adr/) — survive years, change only
  by explicit amendment.
- **Disposable:** per-feature specs in `docs/specs/<feature>/` — they *die on purpose* once the
  feature ships and its behaviour is captured in tests + code. Don't polish a spec you're about to
  delete; make it executable, then let it go.

## The loop

```
        ┌──────────────────────────────────────────────────────┐
        │  /plan  ──►  /implement  ──►  /verify  ──►  ship      │
        │    │             │              │                     │
        │  spec         code to spec   gates + review           │
        │  (human       (domain→app     (ruff, mypy,            │
        │   approves)    →infra→web)     import-linter,         │
        │                                pytest, vitest,        │
        │                                reviewer)              │
        └──────────────────────────────────────────────────────┘
                    ▲                          │
                    └──── NEEDS CHANGES ◄───────┘  (max 3 iterations,
                                                    then escalate to human)
```

### 1. `/plan <feature>` — write the spec, get sign-off

Produces three short files in `docs/specs/<feature>/` from the templates in `docs/specs/_template/`:

- **feature-spec.md** — the *what/why*: ubiquitous language for this slice, user story, in-scope and
  explicit **non-goals**, enumerated measurable **acceptance criteria** (the DoD checklist), and an
  enumerated **failure contract**.
- **technical-plan.md** — the *how*: which bounded context(s), the aggregate/value-object/event/port
  changes, the application use case, the infrastructure adapter, the API contract, the React surface,
  migrations, and what "executable" means for this slice.
- **task-list.md** — ordered, small tasks in canonical order (domain → application → infrastructure →
  wiring → API → frontend → tests).

**A human approves the plan before any code is written.** This is the highest-leverage gate — it is
where wrong intent is cheapest to fix. The plan may not contradict the Constitution or an accepted ADR.

### 2. `/implement` — build the feature to the spec

Work the task list in order. Domain first, always: model the business in pure Python, then the use
case, then the framework wiring, then the React surface. Keep each task small enough to review in
under five minutes. Commit per task on a `feature/<name>` branch. Run `make check` locally before each
commit.

**A slice is not done at the API.** This is a full-stack product; a tailoring endpoint nobody can
click is half a feature. Every user-facing slice ends with the React surface and at least one test
that drives it.

### 3. `/verify` — prove it's done

Run all quality gates (Constitution §6) and the **reviewer agent** (read-only). Verdict is PASS only
with zero CRITICAL and zero MAJOR findings and every acceptance criterion checked. On NEEDS CHANGES,
fix and re-verify; after **3 iterations without PASS, stop and bring it to the human** — repeated
failure means the spec or the design is wrong, not the code.

## Definition of Done

A feature ships only when, per Constitution §6:

1. Ruff clean · 2. mypy `--strict` zero errors · 3. import-linter zero violations ·
4. pytest green with new domain + application/API tests · 5. Frontend `tsc`/eslint/vitest green ·
6. Reviewer PASS · 7. All acceptance criteria checked ·
8. Docs updated (CLAUDE.md / ADR / FORboehpyk.md as needed).

## A note on LLM-dependent features

Most of this product's value passes through a non-deterministic dependency, which breaks the usual
testing reflex. Two rules, from the first slice onward:

- **Never call the real Gemini API in an automated test.** Tests drive a fake `LlmPort`. This is the
  first and best argument for the port existing at all: without it, the suite is slow, flaky, costs
  money, and cannot exercise the failure paths that matter most.
- **Prompt quality is evaluated, not unit-tested.** Keep a small, committed set of fixture postings
  and CVs and a `make eval` run that exercises the real API by hand when the prompt changes (PRD §10
  suggested validation 2). A green `pytest` says the plumbing works; it says nothing about whether
  the output is any good, and pretending otherwise is how you ship a confident, useless feature.

## Branching & commits

- `main` is always releasable. Work on `feature/<name>`; open a PR to `main` even solo — the PR is
  where CI runs and the diff is reviewed. Squash-merge.
- Small, task-sized commits with imperative messages. A commit that changes a decision also amends the
  Constitution/ADR in the same commit.

## Where the pieces live

| Piece | Location |
|---|---|
| Constitution, ADRs | `docs/constitution.md`, `docs/adr/` |
| Per-feature specs | `docs/specs/<feature>/` (from `_template/`) |
| Agents / commands / hooks / skills | `.claude/` — inventory in [tooling.md](./tooling.md) |
| Phase roadmap | [roadmap.md](./roadmap.md) |
| CI/CD | [cicd.md](./cicd.md) + `.github/workflows/` |
| Infrastructure & runbooks | [infrastructure.md](./infrastructure.md) |

## What we deliberately do NOT do

No phase state machine, no `/sprint` orchestrator, no parallel worktrees, no 16 agents. If the project
ever becomes multi-person or regulated, revisit — the SDD playbook scales up from here without rework,
because the durable docs (Constitution, ADRs, executable specs) are the same at any weight.
