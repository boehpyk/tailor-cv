# TailorCraft SDLC Tooling — Agents, Commands, Hooks, Skills

The concrete `.claude/` inventory that operationalizes the [SDLC](./sdlc.md). The **roster is
deliberately lean** — this is solo SDD, not a 16-agent pipeline.

## Design principles

- **Few, sharp agents.** Each has one job and an explicit "what you do NOT do" list.
- **Model tiering.** Strong models design and build; cheaper models test and run infrastructure.
- **Read-only reviewers.** Review/audit agents never Edit/Write — an auditable, drift-free trail.
- **Coaching, not just policing.** Because learning Python and React is a ranked goal
  (Constitution §2), the reviewer *explains the pattern* when it flags a violation.

## Agents (`.claude/agents/`)

| Agent | Model | Role | Writes code? |
|---|---|---|---|
| `planner` | opus | From a request + Constitution/ADRs, writes the three spec files. Identifies bounded context(s), aggregates, ports, and the API + React surface. Stops for human approval. | No |
| `domain-modeler` | opus | Implements the **domain + application** layers: aggregates, value objects, domain events, ports, use cases. Pure Python, stdlib only in `domain/`. The architectural heart. | domain/application only |
| `api-dev` | opus | Implements the **infrastructure** layer: SQLAlchemy imperative mappings and repositories, Alembic migrations, FastAPI routers and Pydantic schemas, Celery tasks, the Gemini/parser/renderer adapters, DI wiring. | infrastructure only |
| `react-dev` | opus | Implements the **frontend**: components, custom hooks, TanStack Query wiring, the TipTap editor tabs, Tailwind styling, the typed API client. | `web/` only |
| `qa` | sonnet | Writes tests **after** implementation: domain unit tests (no I/O), application/API tests against a real database, Vitest component tests. Independent of the implementer. | Tests only |
| `reviewer` | opus | **Read-only.** Checks changed files against Constitution §4/§6/§8, Python/React idiom, and the port boundaries. Returns PASS / NEEDS CHANGES with CRITICAL/MAJOR/MINOR/STYLE findings, each with a one-line *why this pattern matters* (teaching mode). | No |
| `devops` | sonnet | Docker, Compose, nginx, the Makefile, CI/CD, deployment, the footgun guards. | Infra config only |

> The `planner` doubles as the lightweight orchestration point: the human runs `/plan` → approves →
> `/implement` (domain-modeler → api-dev → react-dev → qa) → `/verify` (reviewer). No separate
> always-on orchestrator agent; the commands sequence the work.

## Commands (`.claude/commands/`)

| Command | Does |
|---|---|
| `/new-feature <name>` | Scaffolds `docs/specs/<name>/` from `_template/` and a `feature/<name>` branch. |
| `/plan <name>` | Invokes `planner`; fills the three spec files; presents for human approval. |
| `/implement <name>` | Works the approved `task-list.md` in order via `domain-modeler` → `api-dev` → `react-dev` → `qa`, `make check` per task. |
| `/verify <name>` | Runs all gates (`make check`) + `reviewer`; reports PASS/NEEDS CHANGES; enforces the 3-iteration escalation rule. |
| `/adr <title>` | Creates the next-numbered ADR from the ADR-0000 format. |

## Hooks

**Claude Code hooks** (harness-executed, configured in `.claude/settings.json`) — these are **built
now, not deferred.** The muzbar project listed the same two hooks as a Phase 0 item and still had not
wired them four slices later, while relying on a commit-time gate to catch what a write-time guard
would have caught immediately. The lesson was cheap to import and is imported here.

| Hook | Trigger | Action |
|---|---|---|
| `domain-purity-guard.sh` | `PreToolUse` on Edit/Write/MultiEdit | Blocks a write into `api/src/tailorcraft/domain/` that introduces a third-party import (fastapi, sqlalchemy, pydantic, celery, httpx, …), and blocks a write into `application/` that imports `infrastructure`. Fails **at write time**, not at commit time. |
| `post-edit-check.sh` | `PostToolUse` on Edit/Write to `api/**/*.py` | Runs `ruff check` + `ruff format --check` on the touched file and surfaces the output inline. Fast; the heavy gates stay in `make check`. |

**Git hooks** (tracked in `scripts/git-hooks/`, wired via `make hooks.install` → `core.hooksPath`):

| Hook | Action |
|---|---|
| `pre-commit` | Runs `make check` when the dev stack is up; **blocks** a compose file that publishes a Postgres/Redis port; **blocks** a staged root `.env` or an obvious API key in the diff. |
| `commit-msg` | Enforces imperative mood and a ≤72-char subject. |

## Skills (`.claude/skills/`)

| Skill | Use |
|---|---|
| `hex-slice` | The canonical recipe for a new vertical slice: context choice, value objects, aggregate, event, port, use case, SQLAlchemy imperative mapping, migration, router, wiring. The Python-idiom reference. |
| `gemini-tailoring` | How to change anything that touches the LLM: prompt structure, structured output, the timeout/retry/failure contract, token budgeting, the fake adapter tests use, and how to evaluate prompt quality (which is not a unit test). |
| `react-feature` | How to add a frontend feature here: where state lives (server state → TanStack Query, form state → local, nothing global by default), custom-hook boundaries, the typed API client, the TipTap tab pattern, and the component-splitting rules. |
| `deploy` | The build→push→SSH→migrate deploy runbook + rollback; the footgun checklist as a pre-deploy gate. |

## Build order (Phase 0)

1. `reviewer` + `domain-modeler` + `api-dev` (unblock the core loop).
2. `planner`, `react-dev`, `qa`, `devops`.
3. Commands, then git hooks (`pre-commit` first — it guards everything after), then the Claude hooks.
4. Skills last (they encode patterns you'll refine as the first real slices land).
