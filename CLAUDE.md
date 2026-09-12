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

> **Status: two slices shipped (2026-09-10).** Phase 1 is under way and the architecture is
> carrying weight rather than describing itself.
>
> - **1.1 `intake-base-cv-upload`** (PR #1) — upload a base CV, sniffed by its bytes, extracted in a
>   worker thread, owned by a guest session.
> - **1.2 `posting-job-description-intake`** (PR #2) — paste a job description or hand over a link,
>   fetched behind a guarded egress (ADR-0012) with FR-2's paste fallback as an action.
>
> **473 backend and 66 frontend tests**, green twice in a row. Every gate verified by running it:
> Ruff, mypy `--strict` (142 files), import-linter (3 contracts kept), pytest, `tsc --noEmit`,
> ESLint, Prettier, Vitest, `vite build`, and a production API image that builds, boots as a
> non-root user, and **runs the extractor inside itself** rather than merely importing it.
>
> **CI on GitHub is now verified** — Phase 0 listed it as unproven for want of a remote; `api` and
> `web` both pass on `main`, which also proves the WeasyPrint system libraries and the
> `trafilatura`/`lxml` wheels install there and not only in the dev container. The deploy's **build**
> job passes and pushes images to GHCR.
>
> **The deploy path is still unproven, and one part of it is worse than unproven.** There is no VDS,
> so `deploy` fails at the SSH sync — expected. But the `production` environment has **zero
> protection rules**, so the "manual-gated deploy" this file and `docs/cicd.md` both describe does
> not exist: the job has the `environment:` hook and nothing attached to it. Today a missing
> `SSH_KEY` is what stops a release, which is an accident rather than a control. **Configure a
> required reviewer on the `production` environment before setting the SSH secrets**, or the first
> merge after they land deploys unattended.
>
> Three ADRs were added by 1.2, two of them tracked in git for the first time: `.gitignore` excluded
> all of `docs/`, which meant ADR-0012 — the contract the SSRF adapter was built against — was
> absent from its own review. `docs/adr/**` and `docs/constitution.md` are now tracked; the PRD,
> the specs and the infra notes stay local. **The repository is public**, so anything added to
> `docs/adr/` is published the moment it lands.
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

**A port that promises to translate every failure needs a catch-all, not an allow-list.** Listing the
third-party exceptions you know about (`PdfReadError`, `BadZipFile`, …) is a bet that you enumerated
every way a vendor library can fail on input a stranger chose, and that bet loses: a corruption sweep
found `KeyError`, `AttributeError`, `ValueError` and `LimitReachedError` escaping the extractor into a
500-with-the-file-on-disk. The specific translations go on top, carrying the better reason; an
`except Exception` floor goes underneath making the port's promise true by construction. `Exception`,
never `BaseException` — `asyncio.CancelledError` must still cancel. **Log the exception's type, never
its message or `exc_info`** (a `pypdf` message quotes bytes out of the document), and re-raise
`from None` so the frame holding the upload is unreachable from a Sentry report.

**Outbound HTTP to a caller-chosen host is a guarded egress, not a client**
([ADR-0012](./docs/adr/0012-outbound-http-is-a-guarded-egress.md)). Ten obligations, all of them
non-negotiable, and the ADR is a review checklist rather than a record: the scheme allow-list lives
in a **value object** (so `file:///etc/passwd` is unconstructable, not rejected downstream); DNS
resolves through the loop's `getaddrinfo` **before** connecting; the address policy judges **every**
resolved address, not the first; the socket connects to a **verified** address (IP-pinned, with
`Host` and `extensions={"sni_hostname": …}` keeping TLS verification on the name — measured against
the installed httpx, not assumed); redirects are manual and **every hop re-runs the whole guard**;
`trust_env=False`; timeouts are layered under a hard `asyncio.wait_for`; the byte cap is enforced
**while streaming, on decoded bytes** (never `Content-Length` — a claim by the party you are
defending against); parsing goes in a thread; and an `except Exception` floor guarantees the port's
contract. **There is no off switch** — no setting weakens the address policy, and the testing seam is
a constructor argument with a strict default.

Two things measurement disproved here, both worth knowing before you write the next guard:
`IPv6Address("::ffff:127.0.0.1").is_loopback` is **True** (CPython already unwraps mapped addresses,
so the usual justification for `ipv4_mapped` is wrong — it is load-bearing only for the hand-written
CGNAT range), and **`IPv4Address.is_private` includes link-local**, so `is_private` is not a safe way
to say "private but not `169.254.169.254`".

**A mapped class needs an explicit `__init__`, even an empty one.** `registry.map_imperatively`
installs a default constructor on a mapped class that defines none, and it accepts the **mapped
attribute names** — so `Aggregate(_source=…, _source_url=…)` bypasses your named constructors and
every invariant they enforce. A no-argument `def __init__(self) -> None: ...` restores the guarantee;
a *raising* one breaks the named constructors, because a mapped class must be built through `cls()`
(the instrumentation wrapper is what attaches `_sa_instance_state`).

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
make check.static        # every gate EXCEPT pytest/vitest — the RED commit of a TDD cycle only

# Guest retention (ADR-0006) — rehearse, do not discover. See docs/infrastructure.md.
make purge.dry                                     # report only; deletes nothing
make purge limit=50                                # a small, explicit bite
make purge                                         # a full run
curl -s localhost:8080/health/ready | jq .jobs.guest_purge   # the backlog — the signal to trust

# Job-posting egress (slice 1.2). Bounds live in Settings: POSTING_FETCH_* (timeouts, the 2 MiB
# decoded-byte cap, 3 redirect hops), POSTING_*_RATE_LIMIT_* and JSON_REQUEST_MAX_BYTES. There is
# deliberately NO setting that weakens the SSRF address policy.

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
- **Tests are tiered red-first** (docs/sdlc.md §2). Domain, application, **every row of the failure
  contract**, the HTTP contract and the React loading/error/empty/success states are written
  **before** their implementation, against a skeleton of real signatures with `NotImplementedError`
  bodies, and must be observed failing **on their assertion** — an `ImportError` red proves a file is
  absent, not that the assertion discriminates, so it does not count. The failure is pasted into the
  RED commit body (`Recorded red: …`), which commits with `TDD_RED=1` / `make check.static`; never
  `--no-verify`, which would also drop the secret, PII and port guards. Mappings, repositories,
  migrations, adapters, DI wiring and component markup stay **test-after** on purpose: their shape is
  discovered against the library, so test-first there buys rewrite churn, not confidence. The
  implementer writes the skeleton; `qa` never touches production code. **A test edited in the commit
  that made it pass is the failure this whole cycle exists to prevent** — the reviewer looks for it in
  the history, not the diff.
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
- **`make deps` must sync every container running application code, not just `api`.** `api`,
  `worker` and `beat` all mount the same source and run the same package, so a sync that reaches
  only one of them leaves the others on a venv from whenever they were last built — with no error
  and nothing in a log. Slice 1.3 found `worker` and `beat` still holding a venv from **slice 1.2**;
  it had gone unnoticed because the only new dependency since then (`trafilatura`) is used by the
  posting fetch, which runs in the API. The Gemini call does not — it runs in the worker, which is
  where it would have surfaced as a `ModuleNotFoundError` in a process nobody is watching. This is
  the image-verification lesson one level down: same failure, a venv instead of an image.
- **A startup refusal under `uvicorn --workers N` does not exit the container.** The production
  guard (`MisconfiguredSettings` when `APP_ENV=production` has no `GEMINI_API_KEY`) fires at import in
  every worker process, and uvicorn's multiprocess supervisor respawns the crashing import forever. The
  container never becomes ready and never serves a request — **and never exits**, so
  `restart: unless-stopped` never cycles it and nothing reads as a restart loop. On a real box it
  presents as one traceback logged endlessly. Measured against the production image in slice 1.3 (T36).
  When a release's readiness check never goes green, read the logs for a settings refusal before
  suspecting the network.
- **A dev box with a real `GEMINI_API_KEY` spends money from the UI.** There is no dev-mode fake: the
  worker reads the key and makes a paid call for every tailoring run started at localhost. The test
  suite never reaches it — it replaces the LLM on the worker's own composition-root path and asserts
  the fake was called, because `dependency_overrides` cannot reach the worker — but a manual click does,
  and so does `make eval`. Keep the key out of `.env` unless you mean to spend.
- **`make db.dump` is not a backup of this product.** Restoring rows that point at uploaded files you
  did not restore gives you a broken application with a green restore. Back up the uploads volume
  alongside the database.
- **Any container process that persists state to a relative path writes it into your source tree.**
  Celery Beat's last-run database defaults to `celerybeat-schedule` in the working directory, which
  under the dev bind mount is `./api` — three files landed in a commit before anyone noticed.
  `--schedule` now points at a named volume, which is also where it belongs operationally: lose that
  file on restart and a schedule can double-fire. Check this for every new daemon.
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
- **`proxy_set_header` inheritance in nginx is all-or-nothing.** A block inherits the outer block's
  `proxy_set_header` directives only if it declares **none** of its own. Adding one header inside a
  `location` silently drops every server-level header. It cost a debugging round already: the dev
  config's `location /` set only `Upgrade`/`Connection`, so `Host` fell back to nginx's default of
  `$proxy_host` — the literal upstream name — and Vite refused the request. The tempting fix (add
  the upstream name to Vite's `allowedHosts`) would have hidden a proxy misconfiguration and left
  `X-Forwarded-*` missing too. **Repeat the headers in any block that sets one.**
- **`NODE_ENV=development` in the dev container leaks into `npm run build`.** Vite honours it during
  `build`, so `make web.build` produced a React *development* bundle — 442 kB where production ships
  236 kB. The gate passed while checking an artifact neither CI nor the box ever builds. `make
  web.build` now forces `NODE_ENV=production`. A gate that checks a different thing is worse than no
  gate, because it also supplies confidence.
- **`send_default_pii=False` does not cover traceback frame locals.** `sentry_sdk` defaults
  `include_local_variables=True`, which is a separate setting that neither `send_default_pii` nor
  `max_request_body_size="never"` affects. Any exception escaping a function that holds a CV in a
  local ships that CV to Sentry. Two settings that sound like they cover PII, one that decides it.
- **A failed database write carries its data out through three layers, and `hide_parameters=True`
  covers one.** (1) SQLAlchemy renders `[parameters: (...)]` into every `DBAPIError` — the flag
  removes that. (2) The **driver's own message** is copied in verbatim: asyncpg quotes a value it
  cannot encode, and PostgreSQL's `DETAIL: Failing row contains (...)` on a CHECK violation is the
  whole row, tailored CV included. (3) SQLAlchemy raises `from` the driver's exception, so
  `traceback.format_exception` — what Celery logs — and Sentry's chain walker render every link, and
  a clean `str(exc)` proves nothing. `include_local_variables=False` reaches none of the three; it
  governs frame locals, not messages. The worker lets a failed save of a succeeded run escape on
  purpose (G-28), so this is a real path, not a hypothetical. `persistence/database.py` keeps the
  flag and adds a per-engine `handle_error` listener that withholds the driver's message, keeps
  SQLSTATE and schema identifiers, and cuts the chain — with no setting to turn it off. Found at
  slice 1.3's `/verify`, where the first fix was one flag and a test checking only `str(exc)`
  certified it; the tests now assert on the rendered chain. **PostgreSQL's server log still holds
  the failing row** at the default `log_error_verbosity` — that is the database container's config
  (`terse`), outside the application's reach. Same trap as the bullet above: a setting that sounds
  like it covers PII, and the layers it does not.
- **Alembic's generated `fileConfig(...)` disables every pre-existing logger.** The default is
  `disable_existing_loggers=True`, and it silenced 24 of them here — `pypdf`, `docx`, `celery`,
  `redis`, `sqlalchemy`, `sentry_sdk`, `httpx` — none named in `alembic.ini`. `.disabled`
  short-circuits `isEnabledFor` before the level is read, so it beats anything `observability.py`
  sets. The migration fixture is **session-scoped**, so in the suite this silences those loggers for
  every later test: a privacy test asserting "X never appears in the logs" passes vacuously, and a
  future test asserting something *is* logged fails for a reason nobody finds quickly. `env.py` now
  passes `disable_existing_loggers=False`.
- **Any CPU-bound call in an async route is on the loop, including the ones that "aren't real work".**
  Sniffing a DOCX opens the upload as a zip and reads its whole central directory — a 100,000-entry,
  8.6 MB archive (under the 10 MB cap) stalled the loop **374 ms** for every concurrent user, with no
  error and nothing logged. A rate limit bounds how *often* the loop stalls, never whether it stalls.
- **nginx must not run `ngx_http_realip_module`.** One layer reconstructs the client IP, not two.
  nginx forwards the headers; the application decides. Two trust layers that each look right in
  isolation is the trap, and the symptom is a rate limiter keyed on the proxy's address — one global
  bucket instead of one per visitor.

## SDLC

Lean solo Spec-Driven Development. Loop: **`/plan` → `/implement` → `/verify`.** Every feature gets a
short spec in `docs/specs/<feature>/`. Inside `/implement`, the red-first tiers run
**SKELETON (implementer) → RED (`qa`, failure recorded) → GREEN (implementer)**; the rest is
test-after by design. Details: [docs/sdlc.md](./docs/sdlc.md). Agents/commands/hooks:
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
