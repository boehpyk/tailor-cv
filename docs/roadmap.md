# TailorCraft Roadmap — Phased, Gated Build

The [PRD](./PRD.md)'s three build phases, decomposed into step-by-step milestones for solo,
spec-driven work. Each phase **ends in a validation gate** tied to a PRD success metric or suggested
validation — you do not enter the next phase until its gate is green. Gates are where a solo dev
catches a wrong foundation before building three phases on top of it.

Legend: each milestone is a `/plan → /implement → /verify` cycle (see [sdlc.md](./sdlc.md)).

---

## Phase 0 — Foundation & scaffolding *(new; precedes PRD Phase 1)*

Stand up the skeleton the SDLC assumes. No product features yet.

- [x] **SDLC harness** — Constitution, sdlc/tooling/roadmap/cicd/infrastructure docs, ADRs 0000–0008,
      spec templates, `.claude/` agents + commands + skills + hooks, tracked git hooks. *(2026-09-04)*
- [ ] **FastAPI app** under `api/`, hexagonal packages (`domain/`, `application/`, `infrastructure/`),
      `pyproject.toml` managed by `uv`, `importlinter` contracts expressing the layer rules.
- [ ] **React app** under `web/` — Vite + TypeScript strict + Tailwind v4 + TanStack Query, ESLint,
      Prettier, Vitest.
- [ ] **Docker**: `docker-compose.yml` (prod) + `docker-compose.dev.yml` (dev), api/worker/web images,
      nginx, postgres, redis — footgun guards applied (ADR-0003 / infrastructure.md).
- [ ] **Quality tooling**: Ruff, mypy strict, import-linter, pytest (+ asyncio, real DB rollback),
      the Makefile (`up.dev`, `migrate`, `lint`, `types`, `imports`, `test`, `check`, `db.dump`).
- [ ] **Test environment**: dedicated `tailorcraft_test` database (never the dev DB), transactional
      rollback per test, a fake `LlmPort` that every test uses.
- [ ] **CI** (`.github/workflows/ci.yml`) — the same gate list as `make check`, plus the frontend job.
- [ ] **Health endpoints** — `/health/live` and `/health/ready`; ready probes Postgres **and** Redis
      **and** Celery liveness (never a bare `return "ok"` — see ADR-0005).
- [ ] **Error tracking (Sentry)** — wired in Phase 0, not deferred. See the note below; this project
      has a silent-failure path (a Celery export, a swallowed LLM error) from its *first* slice.
- **Gate:** CI green, `make check` passes locally and in CI, `docker compose` brings the stack up
  healthy, `/health/ready` returns 200 with all three dependencies probed, and the React dev server
  talks to the API through nginx.

> **Note on Sentry.** The muzbar project deferred error tracking out of Phase 0 as "cheap insurance,
> later" and then spent a session debugging a crash-looping worker while every health check reported
> green. TailorCraft's *first shipped slice* already contains the same shape of hazard — an LLM call
> that can fail slowly, a Celery task whose only failure symptom is a download that never appears.
> Wire it in Phase 0. This is the single most valuable thing imported from the previous project.

---

## Phase 1 — Core guest flow & MVP *(PRD Phase 1)*

The whole product, for one anonymous user, end to end.

- [ ] **1.1 `intake-base-cv-upload`** — upload PDF/DOCX/TXT, sniff and validate the real content type,
      extract plain text behind `CvTextExtractorPort`, store the file behind `FileStorePort`.
      *(FR-1, US-1)* First domain code lands here: `BaseCv` aggregate, `ExtractedText`, `FileRef`.
- [ ] **1.2 `posting-job-description-intake`** — accept pasted text **or** a URL; fetch and extract
      main content behind `JobPostingFetcherPort`; SSRF guard on the URL; the flash-message fallback
      when fetching fails. *(FR-2, US-2)*
- [ ] **1.3 `tailoring-generate-documents`** — the Gemini call behind `LlmPort` with structured
      output, a timeout, a bounded retry, and a recorded failure state. Rate-limited from day one.
      *(FR-3, US-3)* This is the slice that has to hit the **< 15 s** budget.
- [ ] **1.4 `workspace-progress-and-editor`** — the React dual-tab entry workspace, the progress
      states (*Extracting CV → Fetching Job → Tailoring*), and the tabbed TipTap editor for CV and
      cover letter. *(FR-4, US-4)*
- [ ] **1.5 `export-multi-format-download`** — TXT and Markdown rendered inline; PDF (WeasyPrint) and
      DOCX rendered in a **Celery task**, polled by the client. *(FR-5, US-5)*
- [ ] **1.6 `retention-guest-purge`** — the Celery Beat job that deletes guest sessions, their files
      and their rows after 24 h, with a backlog count on `/health/ready` and a log line on every run
      *including the ones that delete nothing*. *(FR-6, PRD §9)*
- **Gate:** a friend who is not you tailors a real CV against a real posting and downloads a PDF and
  a DOCX, without help, in under two minutes — and 20 varied CV layouts parse without a crash
  (PRD §10 validation 1). Prompt quality checked against 10 real postings (validation 2).

---

## Phase 2 — Authentication & persistence *(PRD Phase 2)*

- [ ] **2.1 `identity-register-and-login`** — email/password, hashed with argon2, JWT access + rotating
      refresh cookie (ADR-0008). Clean auth state in React (one hook, one source of truth).
- [ ] **2.2 `intake-saved-base-cvs`** — a registered user's base CVs persist and are re-usable; the
      guest purge must not touch them (the retention predicate becomes interesting — write the test
      that proves a registered user's CV survives a purge run).
- [ ] **2.3 `tailoring-application-history`** — past runs listed, re-openable, re-exportable.
- [ ] **2.4 `workspace-registration-cta`** — the post-generation "save your base CV" banner, and the
      **claim flow**: a guest who registers keeps the work they just did. Design this before 2.1
      ships, because it constrains how guest sessions are keyed.
- **Gate:** an account survives a full deploy + migration cycle; a guest→registered upgrade loses
  nothing; the purge job provably spares registered data.

---

## Phase 3 — Tracking & advanced features *(PRD Phase 3)*

- [ ] **3.1** Application tracking dashboard / Kanban board.
- [ ] **3.2** Visual PDF layout templates (a small set, chosen — not a layout engine; Constitution §5).
- [ ] **3.3** Advanced rate-limit / retry UI feedback with exponential backoff surfaced to the user.
- **Gate:** the founder and the friend group use it weekly without touching the database by hand.

---

## Last — deferred operational activations

Things that are *designed and documented* before they are *switched on*. Keep this list short and
never let an item rot here silently.

- [ ] Celery Beat schedule for the retention purge — run by hand until Phase 1.6 has been rehearsed
      on real data (see infrastructure.md's runbook). A purge is a `DELETE`; rehearse it, do not
      discover it.
- [ ] Backups: nightly `pg_dump` + the uploaded-files directory, off the box.
- [ ] Uptime monitor hitting `/health/ready` from outside the VDS.

## Cross-cutting, every phase

- **Every slice updates [FORboehpyk.md](../FORboehpyk.md)** with what broke and what it taught. That
  file is a deliverable of this project, not a byproduct (owner's standing rule).
- **Every slice that touches the LLM budget re-measures it.** A latency budget checked once is a
  latency budget you no longer have.
- **No slice ships without its React surface.** See sdlc.md.
