# TailorCraft Constitution

> The Constitution is the one document that **survives years**. Feature specs die per-feature; this
> does not. It changes only by an explicit, committed edit — never silently in chat. When code and
> Constitution disagree, one of them is a bug: fix the code, or amend this file on purpose.

**Project:** TailorCraft — AI-powered CV & cover-letter customizer
**Owner:** solo developer (boehpyk)
**Status:** Living · established 2026-09-04
**Primary reference:** [PRD.md](./PRD.md)

---

## 1. Mission

A job seeker drops in a base CV and a job posting, and gets back a CV and cover letter tailored to
that posting — editable in the browser, downloadable as PDF, DOCX, Markdown or plain text, in under
two minutes. Guests can do this without an account; registered users keep their base CV and their
application history.

The single sentence to keep in your head: **one base CV in, one tailored application out, fast enough
that a person applies to ten jobs in an evening.**

## 2. Learning objective (first-class)

This project is also a vehicle to learn **idiomatic Python (backend/API/worker design) and React
(frontend architecture, state, component patterns)** in depth. That is not a footnote — PRD §2 ranks
it as a goal, and US-7 / FR-7 make it a *requirement of the codebase*, not a wish.

When two implementations are equally valid for the product but one teaches the pattern more honestly
(a real port instead of a direct SDK call, a custom hook instead of a `useEffect` sprawl, a value
object instead of a `dict`), **choose the one that teaches**. The tooling in this repo is built to
*coach* the architecture, not merely enforce it.

The corollary matters as much: **this codebase is read as an example.** A shortcut here does not just
cost maintenance, it teaches the wrong lesson to the only person who will ever read it.

## 3. Locked technology stack

These are decided. Changing any row requires a new ADR that supersedes the relevant one.

### Backend

| Concern | Choice | ADR |
|---|---|---|
| Language | Python 3.13 (strict typing everywhere) | [0001](./adr/0001-stack-fastapi-react.md) |
| API framework | FastAPI (async routes) | [0001](./adr/0001-stack-fastapi-react.md) |
| Schemas / validation | Pydantic v2 — at the HTTP boundary, never as the domain model | [0002](./adr/0002-hexagonal-layers-enforced-by-import-linter.md) |
| Dependency management | `uv` + `pyproject.toml` + committed `uv.lock` | [0001](./adr/0001-stack-fastapi-react.md) |
| ORM | SQLAlchemy 2.0 — **imperative (classical) mapping**, never declarative on a domain class | [0007](./adr/0007-persistence-conventions-for-domain-aggregates.md) |
| Migrations | Alembic | [0007](./adr/0007-persistence-conventions-for-domain-aggregates.md) |
| Database | PostgreSQL 16 | [0003](./adr/0003-infra-single-vds-compose-traefik.md) |
| Cache / broker / results | Redis 7 | [0003](./adr/0003-infra-single-vds-compose-traefik.md) |
| Background work | Celery 5 (Redis broker) behind a `TaskQueuePort` | [0005](./adr/0005-celery-redis-for-exports-and-purges.md) |
| Scheduled work | Celery Beat — the guest-retention purge | [0006](./adr/0006-guest-retention-and-local-file-storage.md) |
| LLM | Google Gemini API behind an `LlmPort` | [0004](./adr/0004-llm-behind-a-port-gemini-first.md) |
| CV text extraction | `pypdf` / `python-docx` behind a `CvTextExtractorPort` | [0004](./adr/0004-llm-behind-a-port-gemini-first.md) |
| Job posting fetch | `httpx` + `trafilatura` behind a `JobPostingFetcherPort` | [0004](./adr/0004-llm-behind-a-port-gemini-first.md) |
| PDF rendering | WeasyPrint (HTML → PDF), in a Celery worker | [0005](./adr/0005-celery-redis-for-exports-and-purges.md) |
| DOCX rendering | `python-docx` | [0005](./adr/0005-celery-redis-for-exports-and-purges.md) |
| File storage | Local filesystem behind a `FileStorePort` | [0006](./adr/0006-guest-retention-and-local-file-storage.md) |
| Auth | JWT access token + rotating refresh token in an HttpOnly cookie | [0008](./adr/0008-auth-jwt-access-plus-refresh-cookie.md) |
| Lint + format | Ruff (`ruff check` + `ruff format`) | — |
| Static analysis | mypy `--strict` | — |
| Layer enforcement | **import-linter** (the Deptrac of Python) | [0002](./adr/0002-hexagonal-layers-enforced-by-import-linter.md) |
| Tests | pytest + pytest-asyncio, real Postgres, transactional rollback per test | — |

### Frontend

| Concern | Choice | ADR |
|---|---|---|
| Framework | React 19 + TypeScript (strict) | [0001](./adr/0001-stack-fastapi-react.md) |
| Build tool | Vite | [0001](./adr/0001-stack-fastapi-react.md) |
| Styling | Tailwind CSS v4 | [0001](./adr/0001-stack-fastapi-react.md) |
| Server state | TanStack Query — the *only* place server data is cached | [0001](./adr/0001-stack-fastapi-react.md) |
| Rich editor | TipTap (ProseMirror) — one editor instance per tab | [0001](./adr/0001-stack-fastapi-react.md) |
| Routing | React Router — *chosen; installed with slice 1.4, when routes exist* | — |
| Lint / format / types | ESLint + Prettier + `tsc --noEmit` | — |
| Tests | Vitest + React Testing Library | — |

### Infrastructure

| Concern | Choice | ADR |
|---|---|---|
| Reverse proxy / TLS | Traefik (shared, external network) → nginx per app | [0003](./adr/0003-infra-single-vds-compose-traefik.md) |
| Hosting | Single VDS, Docker Compose | [0003](./adr/0003-infra-single-vds-compose-traefik.md) |
| Image registry | GHCR, SHA-tagged immutable images | [0003](./adr/0003-infra-single-vds-compose-traefik.md) |
| CI/CD | GitHub Actions — CI on PR, deploy behind a manual gate | [0003](./adr/0003-infra-single-vds-compose-traefik.md) |

## 4. Architecture principles

1. **Hexagonal (Ports & Adapters), three layers, strictly ordered dependencies.** Backend code lives
   under `api/src/tailorcraft/`:

   | Layer | May import | Must NOT import |
   |---|---|---|
   | `domain` | the standard library only | FastAPI, SQLAlchemy, Pydantic, Celery, httpx — any third party |
   | `application` | `domain` | `infrastructure`, SQLAlchemy, FastAPI, any adapter |
   | `infrastructure` | `domain` + `application` + anything installed | — |

   Enforced in CI, not by good intentions, and by two mechanisms because neither suffices alone
   (ADR-0002): **import-linter** contracts catch upward imports and named vendors, and
   **`tests/unit/test_domain_purity.py`** parses every domain module and rejects anything that is
   not the standard library. The second is the one that fails *closed* — an import-linter forbidden
   list only knows the packages someone remembered to add to it.

   A third mechanism catches it earliest: `.claude/hooks/domain-purity-guard.sh` refuses the edit at
   write time.

   **Pydantic is a third party.** It is superb at the HTTP boundary and it is not a domain model: a
   `BaseModel` in `domain/` means validation semantics, JSON aliases and `model_config` leaking into
   business rules. Domain value objects are frozen `@dataclass`es that validate in `__post_init__`.

2. **Ports keep vendors swappable.** Every external dependency crosses a domain-defined
   `typing.Protocol` (or ABC): `LlmPort`, `CvTextExtractorPort`, `JobPostingFetcherPort`,
   `DocumentRendererPort`, `FileStorePort`, `TaskQueuePort`, `Clock`, plus one repository port per
   aggregate. The concrete adapter lives in `infrastructure/` and is wired in one composition root.
   This is *why* swapping Gemini for another model is a binding change and not a rewrite (ADR-0004).

3. **Bounded contexts** (initial map — refine as the model sharpens):

   | Context | Owns | Key aggregate(s) |
   |---|---|---|
   | `intake` | uploaded base CVs and their extracted text | `BaseCv` |
   | `posting` | job descriptions, pasted or fetched from a URL | `JobPosting` |
   | `tailoring` | the LLM run and its output documents | `TailoringRun` — `TailoredDocument` is a value object on it, not an aggregate ([ADR-0014 §9](./adr/0014-tailoring-runs-on-a-queue-and-is-polled.md), [ADR-0015](./adr/0015-edits-are-a-revision-on-the-run-stored-as-markdown.md)) |
   | `export` | rendering a document to PDF/DOCX/MD/TXT | `ExportJob` |
   | `identity` | accounts, sessions, tokens | `User` |
   | `retention` | the guest 1-day purge (no aggregate — a policy + a port method) | — |

   Ubiquitous language: keep a glossary in each context's spec. A **base CV** is never a "resume" in
   code; a **tailored document** is never an "output"; a **job posting** is never a "job" or a
   "vacancy". Pick the word and hold the line.

4. **The domain never knows it is on the web, in a worker, or in a request.** The same use case runs
   from a FastAPI route and from a Celery task without a branch. If a use case needs to know, that
   knowledge belongs in an adapter.

5. **The React app is a client of a documented API, not a second implementation of the domain.** No
   business rule is re-expressed in TypeScript. The frontend may reformat and validate for UX; the
   API is the authority and re-validates everything.

6. **The LLM is a *fallible* dependency, and the design says so.** Every call has a timeout, a bounded
   retry, and a defined behaviour when it fails or returns unparseable output. A tailoring run that
   fails is a recorded state of `TailoringRun`, never a 500 with nothing on disk.

## 5. Non-goals (hold the line against scope creep)

- **No** automated job application submission and **no** mass scraping (PRD §2 non-goals).
- **No** multi-page graphical layout designer. Structured document exports, styled by a small set of
  templates. Phase 3 at the earliest.
- **No** proxy/anti-bot service to defeat job-board protection. The copy-paste fallback *is* the
  answer (PRD §9 / FR-2).
- **No** microservices, Kubernetes, or multi-node anything. One VDS. One Compose file.
- **No** second LLM provider until Gemini demonstrably cannot meet the quality or latency budget on
  real postings (ADR-0004).
- **No** speculative multi-agent orchestration. Lean SDD (see [sdlc.md](./sdlc.md)).

## 6. Quality gates (Definition of Done for every feature)

A change is **done** only when all of these are green:

1. `ruff format --check` + `ruff check` — clean.
2. `mypy --strict` — zero errors.
3. `lint-imports` (import-linter) — zero layer violations.
4. `pytest` — all pass; new behaviour has `domain` unit tests **and** at least one
   application/API test against a real database. In the red-first tiers (domain, application, the
   failure contract, the HTTP contract, the React state contract — [sdlc.md §2](./sdlc.md)) each test
   was **observed failing on its assertion before the implementation existed**, and that failure is
   recorded in the RED commit body. A test that has never been red has never been proven to
   discriminate.
5. Frontend: `tsc --noEmit` clean, `eslint` clean, `vitest` green.
6. **Reviewer agent** returns PASS (zero CRITICAL, zero MAJOR) — see [tooling.md](./tooling.md).
7. The feature spec's enumerated acceptance criteria are all checked off.
8. Docs updated where behaviour changed (CLAUDE.md, relevant ADR, [FORboehpyk.md](../FORboehpyk.md)).

`make check` runs 1–5 in one command. Local `make check` and CI must stay in lockstep.

## 7. Performance & operational budgets

| Budget | Target | Source |
|---|---|---|
| End-to-end LLM tailoring | **< 15 s** (upload → tailored draft on screen) | PRD §8 |
| Time to a finished application | **< 2 min** vs 30–45 min manual | PRD §8 |
| Export generation success | **> 98 %** without error | PRD §8 |
| API response (non-LLM, non-export) | < 300 ms p95 | this file |
| — **named exception:** intake upload + extraction | **< 3 s p95** server-side, files ≤ 10 MB | ADR-0009 |
| Guest data lifetime | **≤ 24 h**, enforced by a scheduled purge, not by hope | PRD §9 / FR-6 |
| Uptime signal | `/health/ready` probes Postgres + Redis **and** Celery queue liveness | ADR-0005 |

A budget with no measurement is a wish. Each of these gets an assertion or a runbook check before the
phase that depends on it closes.

## 8. Security & privacy principles

- **A CV is a pile of personal data.** Name, address, phone, employment history — often more PII per
  kilobyte than anything else a user will ever upload. Treat every stored CV as sensitive: never log
  its contents, never put it in an error message, never send it anywhere but the LLM provider, and
  delete guest copies on schedule (FR-6). "It's just a text file" is how leaks happen.
- **The LLM provider sees the CV.** That is unavoidable and must be *stated to the user*, not buried.
  Nothing else is sent: no email address, no account id, no other user's content in the same prompt.
- **Untrusted input crosses a validation boundary** before touching the domain. Three inputs are
  security-critical: the **uploaded file** (type, size, and content sniffing — a "PDF" is whatever
  bytes the user sent), the **job URL** (SSRF: no localhost, no private ranges, no redirects into
  them, http/https only), and the **LLM's own output** (it is rendered into HTML and into a PDF —
  treat it as untrusted text, sanitize before render).
- **Defense in depth on the box.** Docker bypasses UFW by writing iptables directly — a `ports:`
  mapping is a firewall hole. Datastores expose no ports in prod; dev binds to `127.0.0.1` only.
  Redis always has a password. (See [infrastructure.md](./infrastructure.md).)
- **Secrets live in the environment, never in source, never in an image layer.** The Gemini API key is
  read in one place (settings) and injected; nothing else reads `os.environ`.
- **Anti-abuse:** the LLM endpoint is the expensive one. Rate-limit tailoring per session and per IP
  from the first slice — an unauthenticated endpoint that costs money per call is a funded DoS.

## 9. How this Constitution is used

- The **`/plan`** command reads this file first; a feature spec may not contradict it.
- The **reviewer agent** treats §4, §6, and §8 as non-negotiable rules.
- Amendments are commits to this file with a message explaining *why*, ideally paired with an ADR.
