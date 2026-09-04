# CLAUDE.md

Guidance for Claude Code working in this repository. Read the [Constitution](./docs/constitution.md)
before planning any feature — it is the source of truth for stack, architecture, and quality gates.

## Project

**TailorCraft** — an AI-powered CV and cover-letter customizer. A job seeker drops in a base CV and a
job posting; the app returns tailored documents, editable in the browser, downloadable as PDF, DOCX,
Markdown or plain text. Guests need no account; registered users keep their base CV and history.
Full product intent: [docs/PRD.md](./docs/PRD.md).

Secondary goal (ranked, not incidental): **learn idiomatic Python and modern React deeply.** FR-7
makes this a requirement of the codebase, not a wish — prefer the implementation that teaches the
pattern honestly.

**Stack:** Python 3.13 · FastAPI (async) · Pydantic v2 (boundary only) · SQLAlchemy 2.0 imperative
mapping · Alembic · Celery 5 + Redis 7 · PostgreSQL 16 · Google Gemini · React 19 + TypeScript ·
Vite · Tailwind v4 · TanStack Query · TipTap · Docker Compose · Traefik · nginx.

> **Status: Phase 0 scaffolded (2026-09-04).** The SDLC harness and the application skeleton both
> exist. `api/` is a FastAPI app with the three hexagonal packages, `uv`-managed dependencies, an
> Alembic environment and a health endpoint that probes Postgres, Redis **and Celery**. `web/` is a
> React 19 + TypeScript + Vite + Tailwind v4 + TanStack Query app rendering the dependency report.
>
> All gates were verified by running them: Ruff, mypy `--strict` (41 files), import-linter (3
> contracts kept), pytest (27 passed — twice in a row), `tsc --noEmit`, ESLint, Prettier, Vitest (4
> passed), `vite build`, and a production API image that builds and boots as a non-root user. Not yet
> verified: a CI run on GitHub (no remote), and the deploy path (no VDS or domain).
>
> **There is still no product code.** No aggregate, no use case, no migration — `domain/` holds
> `Clock`, `DomainEvent` and the error base, and nothing else. The first slice is roadmap 1.1,
> `intake-base-cv-upload`.
>
> The harness was ported from the muzbar.com project's SDLC and adapted to this stack. Four things
> were changed deliberately rather than copied, each because of a documented failure there: the
> Claude Code hooks are **wired now** instead of listed as a to-do; **Sentry is in Phase 0** instead
> of deferred; `/health/ready` **probes Celery** and not only the datastores; and the deploy
> **verifies the running image of every container**, not just the web one.

## Architecture — non-negotiable

Hexagonal (Ports & Adapters). Backend code lives under `api/src/tailorcraft/` in three layers with
strictly ordered dependencies:

| Layer | May import | Must NOT import |
|---|---|---|
| `domain` | the standard library only | FastAPI, SQLAlchemy, **Pydantic**, Celery, httpx — any third party |
| `application` | `domain` | `infrastructure`, SQLAlchemy, FastAPI, any adapter |
| `infrastructure` | `domain` + `application` + anything installed | — |

Enforced by **import-linter** in CI and by a **PreToolUse hook that blocks the write** before you get
that far (`.claude/hooks/domain-purity-guard.sh`). Ports are `typing.Protocol`s in
`domain/<context>/ports.py`; adapters live in `infrastructure/`. Bounded contexts: `intake`,
`posting`, `tailoring`, `export`, `identity`, `retention` — see Constitution §4.

Canonical order to add a feature: **domain** (value objects → aggregate → events → port) →
**application** (use case) → **infrastructure** (mapping, repository, migration, adapter, router,
wiring) → **frontend** (API client → query hook → components) → **tests**.

**Pydantic is a third party and stays out of `domain/`** ([ADR-0002](./docs/adr/0002-hexagonal-layers-enforced-by-import-linter.md)).
It is superb at the HTTP boundary and it is not a domain model: a `BaseModel` in `domain/` drags
validation semantics, JSON aliases and `model_config` into business rules. Domain value objects are
frozen dataclasses validating in `__post_init__`. This is the most likely accidental violation in the
codebase, which is why it is machine-enforced rather than remembered.

**Persistence conventions** ([ADR-0007](./docs/adr/0007-persistence-conventions-for-domain-aggregates.md)):

- SQLAlchemy **imperative mapping only**. A `Table` plus `registry.map_imperatively()` in
  `infrastructure/persistence/mapping/<context>/<aggregate>.py`. **Never `class X(Base)`, never
  `mapped_column` on a domain class** — that is the tutorial path and it ends the design.
- Value objects map through `TypeDecorator`s, not composites.
- Tables are `<context>_<aggregate>`, singular (`intake_base_cv`), every column named explicitly.
- Column types are **chosen**: JSON is `JSONB`; timestamps are `TIMESTAMP WITH TIME ZONE` and
  **whole-second** — the `Clock` port is whole-second by contract and the system implementation
  truncates at the source, so a database round trip can never change a value. Test doubles must
  honour that too, or an equality assertion fails by microseconds on a bad day.
- Identity is **application-assigned UUIDv7** from `repository.next_identity()`. The aggregate is
  valid before it meets the database — that is what makes domain tests possible without one.
- **Alembic autogenerate is a draft.** Read every line. Migrations are additive and
  backward-compatible (expand → migrate → contract); the deploy runs two versions briefly.

**The LLM boundary** ([ADR-0004](./docs/adr/0004-llm-behind-a-port-gemini-first.md)): everything
non-deterministic or external crosses a port — `LlmPort`, `CvTextExtractorPort`,
`JobPostingFetcherPort`, `DocumentRendererPort`, `FileStorePort`, `TaskQueuePort`, `Clock`. The port
speaks the *domain's* language: `LlmPort` mentions no Gemini, no HTTP, no retries, no JSON. Nothing
outside `infrastructure/llm/` imports the SDK. Every call has a timeout, a bounded retry, and a
defined behaviour for four failures: unavailable, rate-limited, refused, and **output that parses but
is wrong**. A failed run is a recorded state of `TailoringRun`, never a 500 with nothing on disk.

**Structured output is re-validated on receipt.** "We asked the model for JSON" and "this is valid
JSON with the fields we need" are different claims, and the gap between them is where the 2 a.m. bug
lives.

**Background work** ([ADR-0005](./docs/adr/0005-celery-redis-for-exports-and-purges.md)): TXT and
Markdown render inline (they are string manipulation); **PDF and DOCX go to a Celery worker**. The
rule is the cost of the work, not the tidiness of treating all four alike. A Celery task is a **thin
entry point** — resolve, call the use case, translate the outcome — exactly like an HTTP route. No
business logic in a task, and every task idempotent, keyed on the job id.

**Async is load-bearing, and violating it is silent.** A synchronous CPU-bound call inside an async
route blocks the event loop for *every* concurrent user. WeasyPrint, pypdf and python-docx are all
synchronous. It looks fine with one user and collapses with five, and it presents as "the app is
slow", never as an error — which is why the reviewer treats it as CRITICAL rather than a style note.

## Commands

All commands run inside the Docker containers. (Makefile targets wrap them — run `make help`.)

```bash
# Stack
make up.dev              # docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
make down.dev
make up.prod             # no override file — never auto-loaded
make logs c=api
make shell               # bash in the api container

# Database
make migrate                                  # alembic upgrade head
make migration.make name="add base cv"        # autogenerate — THEN READ EVERY LINE
make db.dump                                  # pg_dump -Fc to backups/ (database only; NOT uploads)

# Quality gates (the Definition-of-Done chain; must match .github/workflows/ci.yml)
make lint                # ruff format --check + ruff check
make types               # mypy --strict
make imports             # import-linter — the proof the domain stayed pure
make test                # pytest (opts: k=, file=) against tailorcraft_test
make web.check           # tsc --noEmit + eslint + vitest + vite build
make check               # all of the above — run before every commit

# Guest retention (ADR-0006) — rehearse, do not discover. See docs/infrastructure.md.
make purge.dry                                     # report only; deletes nothing
make purge limit=50                                # a small, explicit bite
make purge                                         # a full run
curl -s localhost:8080/health/ready | jq .jobs.guest_purge   # the backlog — the signal to trust

# LLM evaluation — NOT a test. Calls the real API, costs money, is not in `make check`.
make eval

make hooks.install       # git config core.hooksPath scripts/git-hooks
```

## Conventions

- **Python:** `from __future__ import annotations`; full type hints; `mypy --strict` clean; frozen
  dataclasses with `slots=True` for value objects; `typing.Protocol` for ports; no `Any` without a
  comment justifying it; no mutable default arguments; no dead code; no secrets in source.
- **Domain purity:** zero third-party imports in `domain/`. Persistence and validation are
  infrastructure concerns. Model the business, not the table and not the wire format.
- **No base class for two aggregates that merely share a shape.** Shared shape is not shared
  behaviour, and a base class guesses at rules that differ. Where a new type deliberately contradicts
  an existing one, **comment why at the point of contradiction** — a reader who spots the
  inconsistency should find the reason, not "fix" it.
- **Config:** `os.environ` is read in exactly **one** place (the settings object). A config value that
  can be read from anywhere will eventually be read from the domain layer.
- **Validation:** untrusted input is validated at the infrastructure boundary before reaching the
  domain. Three inputs are security-critical: the **uploaded file** (sniff the content, do not trust
  the filename or the client's content-type; cap the size; store under a generated name), the **job
  URL** (SSRF — scheme allow-list, no private ranges, re-check redirects), and the **LLM's own
  output** (it is rendered into HTML and a PDF; sanitize it).
- **React:** server state lives in TanStack Query and is never copied into `useState`. `useEffect` is
  for synchronizing with something outside React — not fetching, not deriving. Every networked screen
  renders loading, error and empty deliberately, and the error state distinguishes "still working"
  from "this failed" (a user who cannot tell will refresh and pay for a second LLM call). No `any`,
  no `!` to silence the compiler, no access token in `localStorage`, no business rule re-implemented
  in TypeScript.
- **Tests:** `domain` gets pure unit tests with no I/O at all — they should be the fastest and most
  numerous in the suite, and if they are hard to write without a database, logic has leaked outward.
  Application and API tests run against a **real** Postgres with transactional rollback, against
  `tailorcraft_test` — **never** the dev DB. **No test calls the real Gemini API**; CI has no key.
  Time comes from a fake `Clock`, never `datetime.now()`.
  **The database transaction does not roll back Redis.** Rate limiters, the purge heartbeat and the
  purge lock survive between tests and must be cleared in the fixture. The cheap proof you got it
  right is to **run the suite twice in a row** — a second run that fails is the classic symptom. A
  leftover *lock* is the dangerous one: the job then does nothing, logs "skipped", and exits 0, so a
  test asserting a successful run passes against a run that never happened.
- **A test encodes what the code *should* do — never what it was observed doing.** A test written by
  running the code and recording the answer has no source of truth independent of the code, so it can
  never disagree with it. When an acceptance criterion and the implementation disagree, **fix one of
  them on purpose and say which won** — do not write the test that ratifies the accident. And a
  docblock claiming coverage the assertion cannot deliver is the same defect one level up: if a test
  says it guards a regression, verify it fails when you reintroduce that regression.

## Privacy — this product's specific hazard

**A CV is a pile of PII**: name, address, phone, employment history — often more personal data per
kilobyte than anything else a user will ever upload, handed over by someone who is unemployed and in
a hurry. Constitution §8 applies to every slice:

- **Never log CV text, prompt bodies or completions.** Log ids, sizes, durations, token counts,
  outcomes. A debug log is the easiest way to leak a stranger's address into a file nobody thinks of
  as a database.
- **Never put a CV body in a domain event payload** — an event reaches every listener and every log
  line.
- The LLM provider sees the CV. That is unavoidable and is **stated to the user**, not buried.
  Nothing else is sent: no email, no account id, no other user's content in the same prompt.
- **Guest data lives ≤ 24 h** and a scheduled job enforces it (FR-6,
  [ADR-0006](./docs/adr/0006-guest-retention-and-local-file-storage.md)). Registered users' data is
  never touched by that job, and the test proving it is written in the same slice as the job.
- **A guest session is not a weak login.** Owning a session id is not authority over an object that
  references it — check the link, in the use case, every time.

## Infrastructure footguns (baked-in guards)

Documented failure modes we design against (see [docs/infrastructure.md](./docs/infrastructure.md)):

- **Traefik must be pinned to its network:** `--providers.docker.network=traefik`. Otherwise: silent
  30-second 504, no logs anywhere.
- **Docker bypasses UFW** — it writes iptables directly, so a `ports:` mapping is a hole in a firewall
  you believe is closed. Datastores publish no ports in prod; dev binds `127.0.0.1` only. A
  pre-commit hook flags violations in both directions.
- **Named volumes only** — an anonymous Postgres volume remembers a stale password and you will spend
  an hour proving a correct password wrong.
- **Redis always has a password.** It is the broker, the result backend, the cache and the
  rate-limiter store: four load-bearing jobs in one process that is unauthenticated by default. Redis
  down is not "slower", it is "no exports and no purges".
- **`/health/ready` probes Postgres, Redis AND Celery.** A stopped worker otherwise looks *exactly*
  like a healthy system — the API answers, the database answers, and every export queues forever.
  This is a lesson imported from the previous project, where a crash-looping worker sat behind a
  green dashboard for an entire session.
- **Every container running application code appears in the deploy's `pull` list and in the
  image-verification loop** — here `api`, `worker`, `beat`. In the previous project the worker was in
  neither and ran a stale image for four releases; the only symptom was behaviour not matching the
  source. The worker and beat are also **stopped across the migration window**, not merely restarted
  after it.
- **`env_file:` outranks the image's `ENV`, so the root `.env` decides `APP_ENV` on a real box.**
  `.env.example` therefore defaults to `APP_ENV=production` (the safe value) and
  `docker-compose.dev.yml` pins `APP_ENV: dev` in `environment:` (which outranks `env_file:`), so
  dev-ness follows the override file you load rather than a value someone remembered to change.
- **WeasyPrint needs system libraries at runtime** (Pango, Cairo, HarfBuzz, fontconfig, and actual
  fonts). Without them the import succeeds and the *first render* fails — in the worker, where nobody
  is watching. They are installed in `docker/api/Dockerfile` **and** in CI; keep the two in step, and
  remember that a PDF rendered on a box with no fonts is a page of boxes.
- **The uploads volume is mounted by both `api` and `worker`.** The api stores the upload, the worker
  reads it to render. Lose it on either and exports break in a way no health check sees. It is also
  the one assumption that must die first if this ever runs on two boxes — which is why every access
  goes through `FileStorePort`.
- **`make db.dump` is not a backup of this product.** Restoring rows that point at uploaded files you
  did not restore gives you a broken application with a green restore. Back up the uploads volume
  alongside the database.
- **The venv lives at `/opt/venv`, not `/app/.venv`.** The dev override bind-mounts `./api` over
  `/app`, so a virtualenv at uv's default location is shadowed by whatever the host has there — and
  a host venv points at a host interpreter path that does not exist in the container. `UV_PROJECT_ENVIRONMENT`
  moves it out of the mount's way. The symptom otherwise is a missing-interpreter error that reads
  like a corrupt image rather than a mount collision.
- **pytest-asyncio's two loop scopes must agree.** An asyncpg connection is bound to the event loop
  it was created on. A session-scoped engine plus pytest-asyncio's default per-test loop produces
  `RuntimeError: got Future attached to a different loop` on teardown — **from tests that pass
  individually and fail only when run together**, which is the worst way to meet a bug. Both
  `asyncio_default_fixture_loop_scope` and `asyncio_default_test_loop_scope` are `session`.
- **`TC001`/`TC002`/`TC003` are disabled and must stay disabled.** They move imports "only used in
  annotations" into `if TYPE_CHECKING:`. FastAPI, Pydantic and SQLAlchemy all resolve annotations at
  **runtime** — FastAPI calls `get_type_hints()` to decide what to inject — so obeying those rules
  breaks dependency injection with a `NameError` at startup.
- **nginx must not run `ngx_http_realip_module`.** One layer reconstructs the client IP, not two.
  nginx forwards the headers; the application decides. Two trust layers that each look right in
  isolation is the trap, and the symptom is a rate limiter keyed on the proxy's address — one global
  bucket instead of one per visitor.

## SDLC

Lean solo Spec-Driven Development. Loop: **`/plan` → `/implement` → `/verify`.** Every feature gets a
short spec in `docs/specs/<feature>/`. Details: [docs/sdlc.md](./docs/sdlc.md). Agents/commands/hooks:
[docs/tooling.md](./docs/tooling.md).

**A slice is not done at the API.** This is a full-stack product; a tailoring endpoint nobody can
click is half a feature. Every user-facing slice ends with its React surface.

**Branching:** GitHub Flow — `feature/<name>` (matching the spec folder) → PR → squash-merge to
protected `main` → build images → **manual-gated** deploy. Never `git pull` on prod. See
[docs/cicd.md](./docs/cicd.md).

## Documentation duties

When behaviour changes, update: this file (if a convention or command changed), the relevant ADR (if
a decision changed), and [FORboehpyk.md](./FORboehpyk.md) — the running plain-language project story,
capturing bugs hit and lessons learned, per the owner's standing rule.
