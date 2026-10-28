# CLAUDE.md

Guidance for Claude Code working in this repository. Read the [Constitution](./docs/constitution.md)
before planning any feature — it is the source of truth for stack, architecture, and quality gates.

## Project

**FixCV** (`fixcv.app`; the code, images, env vars and databases keep the code name `tailorcraft`) — an AI-powered CV and cover-letter customizer. A job seeker drops in a base CV and a
job posting; the app returns tailored documents, editable in the browser, downloadable as PDF, DOCX,
Markdown or plain text. Guests need no account; registered users keep their base CV and history.
Full product intent: [docs/PRD.md](./docs/PRD.md).

Secondary goal (ranked, not incidental): **learn idiomatic Python and modern React deeply.** FR-7
makes this a requirement of the codebase, not a wish — prefer the implementation that teaches the
pattern honestly.

**Stack:** Python 3.13 · FastAPI (async) · Pydantic v2 (boundary only) · SQLAlchemy 2.0 imperative
mapping · Alembic · Celery 5 + Redis 7 · PostgreSQL 16 · Google Gemini · React 19 + TypeScript ·
Vite · Tailwind v4 · TanStack Query · TipTap · Docker Compose · Traefik · nginx.

## Status

Twenty-one slices have shipped: 1.1–1.6, 2.1–2.5, 3.1–3.3, 4.1–4.4, `__Host-tc_guest`,
`web-design-refresh` and `domain-fixcv-app`. The latest is **`domain-fixcv-app`** (production moves
to `fixcv.app`, config only, ADR-0038, PR #40, `8468b5f`), released 2026-10-27 (run 40591221234).
Before it, **`web-design-refresh`** (a frontend-only reskin plus the static `/welcome` page,
ADR-0036/0037, PR #37, `a539040`) was released 2026-10-27 (run 40539367985), **before** OQ-17's
2026-10-30 floor, by the owner's decision. So **Phase 4's two-week gate clock (to
2026-10-29) closes on the reskinned `/admin`**, not on the code it started on. Its follow-up, PR #38
(`56fabc4`, run 40548606371), mounts the footer's System status for admins only; `/health/ready`
itself stays public. Phase 2 is closed (gate met 2026-10-03). **Phase 1's gate is still unrecorded**
(OQ-7). Each slice's record (ADRs, migrations, measurements, `/verify` rounds, carried
items) is in **[HISTORY.md](./HISTORY.md)**, and the stories are in [FORboehpyk.md](./FORboehpyk.md).
Read the relevant entry before you touch that slice's code.

**Production:** `fixcv.app` (ADR-0038; `www` is a Traefik redirect, `cv.samolit.com` is retired),
served by a single VDS through the Traefik already on the box (external `traefik` network,
entrypoint `websecure`, resolver `le`). Stack in
`/home/boehpyk/www/tailor-cv`, deployed as `boehpyk`. The root `.env` on the box is hand-made, mode
600, `APP_ENV=production`, `TRUSTED_PROXY_HOPS=2`, and **`GUEST_PURGE_ENABLED=false`** (the purge is
run from `/admin/storage`). Mail is Resend over SMTP. The deploy secrets are **`production`
environment secrets** behind `required_reviewers` + `branch_policy`, so **every merge to `main` is a
real release waiting for approval**. A run uses the workflow file **from its own commit**. A control
nobody has verified is a belief: check with `gh api repos/<owner>/<repo>/environments/production`.

**Open items worth knowing:** OQ-16 (`tc_refresh` planted by a sibling host) is closed **by
condition**: no `*.fixcv.app` name but the `www` redirect ever points at an HTTP server; before
one does, ship the `__Host-` rename first (ADR-0008 (m)). 2.3's posting lock renders
`FOR NO KEY UPDATE` where `FOR KEY SHARE` was meant (`tailoring_run.py`, left for the owner).
`/health/ready` takes ~2.1 s because of `control.ping(timeout=2.0)` with no `limit`. Traefik's
access log has not been checked for query strings. Firefox's first manual pass was
web-design-refresh's T42 (2026-10-27).

**The repository is public.** `docs/adr/**` and `docs/constitution.md` are tracked; the PRD, specs,
roadmap and infra notes stay local.

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
`posting`, `tailoring`, `export`, `identity`, `retention`, `tracking` (3.1) — see Constitution §4.

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

**One credential per route — except a named transfer route**
([ADR-0008](./docs/adr/0008-auth-jwt-access-plus-refresh-cookie.md) amendments (f), (g)). A route
answers to the bearer **or** the guest cookie. A transfer route carries data between the two
principals: it reads both, and **each authorizes only its own half** — nothing ever asks "a user
*or* a guest?". The one there is, 2.4's `POST /api/me/guest-work/claim`, takes the bearer as a
dependency (the destination, authorized first) and reads `__Host-tc_guest` **in the handler body**
(the source): never through `require_guest_session`, whose 401 would break the claim's idempotency, and
never through `resolve_or_start_guest_session` — **a transfer route never mints**. The dependency
walker cannot see a call inside a body, so an **AST scan** of `routers/*.py` pins the exception set
to exactly `{POST /api/me/guest-work/claim}`. 2.2's copy route, the first member, was retired in 2.4
(ADR-0022 (d)); a new member is added to that set on purpose, never by drift.

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
make web.check           # tsc -b --noEmit + eslint + prettier + vitest + vite build
make check               # all of the above — run before every commit
make check.static        # every gate EXCEPT pytest/vitest — the RED commit of a TDD cycle only

# Guest retention (ADR-0006) — rehearse, do not discover. See docs/infrastructure.md.
# Unlinks rows AND files; since slice 1.5 the file half includes rendered exports (PDF/DOCX) next
# to uploads, so the backlog count below reads higher after that slice than it used to for the
# same number of guest sessions — not itself a sign of anything wrong.
make purge.dry                                     # report only; deletes nothing
make purge limit=50                                # a small, explicit bite (one batch, not a loop)
make purge                                         # a full run — loops batches until empty
# Each batch line reads `examined, deleted, skipped, failed`. `skipped` (2.4) is a session already
# gone when its DELETE ran (a user claimed it, or another run took it): its files are left alone on
# purpose. It should read about 0, and nothing needs doing when it doesn't.
curl -s localhost:8080/health/ready | jq .jobs.guest_purge   # the backlog — the signal to trust

# Orphans: files with no row (the crash window's survivors). Operator-run only, never on beat.
# Fails CLOSED — if the database cross-check cannot run, it deletes nothing and exits 1.
python -m tailorcraft.cli purge-guests --orphans --dry-run
# Exit codes: 0 success (including deleting nothing) · 1 failed · 2 usage · 3 THE LOCK WAS HELD.
# 3 is the point: a run that did nothing because another holds the lock must not exit 0.

# The same from the browser (slice 4.4, ADR-0035): /admin/storage, admins only. The CLI above stays
# the break-glass; backups (`make db.dump` + the uploads volume) stay an SSH step.
#   make purge.dry       → Preview purge   (GET  /api/admin/guest-purge?limit=; writes nothing)
#   make purge limit=N   → Run purge       (POST /api/admin/guest-purge {"limit": 1..100}; ONE batch)
#   --orphans --dry-run  → Preview sweep   (GET  /api/admin/orphan-sweep; writes nothing)
#   --orphans            → Run sweep       (POST /api/admin/orphan-sweep {"limit": N | null})
#   (no CLI twin)        → the storage table (GET /api/admin/storage: counts + bytes per bucket)
# A held lock is 409 `purge_in_progress` (the CLI's exit 3): nothing ran, wait and try again.
# A preview's would-delete count is `examined`; its `sessions_deleted` is always 0.
# One session early: /admin/guests/<id> → Delete guest session (the purge's own step).

# Identity (slice 2.1). THE BREAK-GLASS: deletes every login, signing every user out. Dry run first.
# Rotating JWT_SIGNING_KEY is NOT this — it logs nobody out. Either way, access tokens already
# issued stay valid until they expire (≤ 15 min). No make target, on purpose: not a routine step.
python -m tailorcraft.cli revoke-logins --all --dry-run   # the count; deletes nothing
python -m tailorcraft.cli revoke-logins --all             # 0 ok (incl. zero) · 1 db failure · 2 usage
python -m tailorcraft.cli check-settings                  # every startup refusal, once; exit 1 + the
                                                          # sentence (never a value), or `settings ok`

# Account erasure (slice 2.2) — the operator path; no password (a user who forgot theirs has no
# reset). Rows committed, then files. NEVER `DELETE FROM identity_user` by hand: the cascade takes
# the rows and orphans every saved file. Dry run first. No make target, like revoke-logins.
python -m tailorcraft.cli erase-account --user-id <uuid> --dry-run   # counts; deletes nothing
python -m tailorcraft.cli erase-account --user-id <uuid>
# Since 2.3 the account's history goes too, and each line appends it after 2.2's unchanged prefix;
# since 3.1 the board's cards are appended after that (no files, so N file(s) is unchanged):
#   would erase account <id>: N saved CV(s), N file(s), N login(s) (dry run); history: N tailoring run(s), N job posting(s), N export job(s); tracking: N tracked application(s)
#   erased account <id>: N saved CV(s), N file(s) unlinked, N failed; history: N tailoring run(s), N job posting(s), N export job(s), N file(s) in all; tracking: N tracked application(s)
# The dry run's file count includes derived export files. A history entry a user deletes in the UI
# goes the same way (rows committed, then files); an unlink that fails leaves an orphan for the sweep.
# Exit: 0 erased (incl. with unlink failures — stderr names the orphan sweep) · 1 no such account,
# a database failure, or SELECT current_database() ≠ the database DATABASE_URL names (refused
# before any read) · 2 usage. Logs ids, counts and class names only.

# Admin roles (slice 4.1, ADR-0032) — the ONLY way a role changes; no HTTP writer, no make target.
# By id, never email (read it from GET /api/auth/me). Dry run first. Takes effect on the next request.
python -m tailorcraft.cli grant-role  --user-id <uuid> --dry-run   # would change user <id>: role user → admin (dry run)
python -m tailorcraft.cli grant-role  --user-id <uuid>             # user <id>: role user → admin
python -m tailorcraft.cli revoke-role --user-id <uuid>             # the last admin may be revoked; recovery is grant-role
# Exit: 0 changed or already held (no event) · 1 no such account / db failure / foreign database · 2 usage.

# Disable / enable an account (slice 4.2, ADR-0033) — BROWSER ONLY, in /admin/users/<id>. There is
# no CLI for either (OQ-19). Disable deletes the account's logins; its open tabs are refused 403
# account_disabled on their next request. If the only admin disables itself, recovery is
# `grant-role` on another account, which then enables the first from /admin. Erase is also in
# /admin now; `erase-account` above stays the break-glass.

# Job-posting egress (slice 1.2). Bounds live in Settings: POSTING_FETCH_* (timeouts, the 2 MiB
# decoded-byte cap, 3 redirect hops), POSTING_*_RATE_LIMIT_* and JSON_REQUEST_MAX_BYTES. There is
# deliberately NO setting that weakens the SSRF address policy.

# Account mail (slice 2.5). Dev and CI deliver to Mailpit, never to a real inbox: the dev override
# pins MAIL_SMTP_HOST=mailpit / PORT=1025 / SECURITY=none in environment:, outranking .env.
make mail.ui             # prints the Mailpit UI URL (127.0.0.1:8025; SMTP is unpublished)
make compose.mail.check  # the dev mail-wiring guard (also a pre-commit check on staged compose files)
make nginx.referrer.check  # Referrer-Policy: no-referrer on the SPA + the meta tag (also pre-commit)
# The identity token sweep runs hourly on beat, always on. Its fact, like the purge's:
curl -s localhost:8080/health/ready | jq .jobs.identity_token_sweep   # overdue should read 0

# PDF layout previews (slice 3.2). Regenerate after ANY change to infrastructure/export/layouts.py,
# then commit the three WebPs and api/tests/fixtures/layout_previews.json with the stylesheet — the
# AC-38 drift test is red until you do. Dev stack up; needs network (a throwaway debian container).
make layout.previews

# LLM evaluation — NOT a test. Calls the real API, costs money, is not in `make check`.
make eval

make hooks.install       # git config core.hooksPath scripts/git-hooks
```

## Conventions

The evidence, commit ids and measurements behind each line are in **[LESSONS.md](./LESSONS.md)**.

- **Python:** `from __future__ import annotations`; full hints; `mypy --strict` clean; value objects
  are frozen `slots=True` dataclasses; ports are `typing.Protocol`; no `Any` without a comment; no
  mutable defaults, dead code or secrets in source.
- **Domain purity:** no third-party imports in `domain/`. A domain module may not import a **sibling
  context** either. Retention reaches other rows through its own ports and plain values.
- **No base class for aggregates that merely share a shape.** Where a type deliberately contradicts
  another, comment why at that point.
- **Config:** `os.environ` is read only in the settings object.
- **Validation:** untrusted input is validated at the boundary. Three inputs are critical: the
  uploaded file (sniff bytes, cap size, generated name), the job URL (SSRF), and the LLM's output
  (sanitize before HTML/PDF).

**React**
- Server state lives in TanStack Query, never copied into `useState`. `useEffect` only syncs with
  the outside world. Every networked screen renders loading, error and empty, and "still working"
  is distinct from "failed". No `any`, no `!`, no business rule duplicated in TS.
- **The credential is not server state:** one module store read via `useSyncExternalStore`. Never
  put it in `useState`, the query cache or browser storage.
- **Scope decides whose workspace, not "is someone signed in?"** Paths, credential and keys come
  from `useScopeMap()`; build every link from `runLink(scope)`.
- **Reads retry, writes hold** (ADR-0031). A held deadline is component state derived during render
  (`useErrorHold`/`useHold`). Word times in the browser's locale.
- **A visual value is an `@theme` token** (ADR-0037): no `style=`, colour literal or arbitrary colour
  class in `.tsx` (`design.grep.test.ts`); text colours come from ADR-0037's contrast table.
- **Fonts are `@fontsource`, never a CDN** (ADR-0036). The latin `@font-face`s are declared in
  `index.css` (fontsource's CSS would ship a `.woff` twin per face); a `.woff2` is never inlined.
- **`npm run build` ends with `check-dist-hosts.mjs`**: a third-party host in `dist/` fails the
  build. A new host is read first, then allow-listed with its reason.
- **Responsive is one DOM**: the nav and the export bar exist once and `max-sm:` moves them. Two
  copies duplicate every accessible name; a closed sheet is `invisible`, never `hidden`.
- TanStack traps:
  - `gcTime: 0` collects unobserved `setQueryData`.
  - `isPending` lags a tick, so guard double submits with `isMutating`.
  - Default structural sharing matches array items by index, so use `shareCardsById` for
    re-sorting lists.
  - v5 refuses `undefined` data; a 204 probe returns `true`.
  - `removeQueries({exact: true})` keeps children, so drop a subtree by prefix.
  - An `onSettled` that *returns* invalidations keeps the mutation pending.
- The app's client turns off `refetchOnWindowFocus`, so **build test clients with
  `createAppQueryClient()`**. `focusManager` listens on `window`, not `document`.
- Lazy routes: the main-bundle half imports nothing from the chunk's modules. Descendant routes in a
  lazy chunk use `useRoutes`, not `<Routes>`. Check with a grep of `dist/assets/index-*.js`.
- Navigate-then-sign-out races `RequireAuth`, because `RouterProvider` ignores `flushSync`.
  Work that must survive an unmount runs before the sign-out.
- A live region mounted together with its text may never be announced: mount it empty, then fill it.
- Fake timers: `advanceTimersByTimeAsync`, flush TanStack notifies with `advance(1)`, reset
  `onlineManager` in `afterEach`, and use `fireEvent` instead of `userEvent.type`.
- Test gotchas: a disabled control satisfies `findByRole`, so wait for enabled. Count `<h1>` inside
  `main`. A page-level `findByText(/^Loading/)` matches the Suspense fallback. A fake's 403
  `account_disabled` signs the tab out (use 503 to fail a section).

**Tests — the red-first discipline** (docs/sdlc.md §2)
- Domain, application, every failure-contract row, the HTTP contract and React states are written
  **before** the code, against a `NotImplementedError` skeleton. They must fail **on their
  assertion** (an `ImportError` doesn't count). Paste the failure into the RED commit
  (`Recorded red: …`, `TDD_RED=1` / `make check.static`). **Never `--no-verify`.** Mappings,
  repositories, migrations, adapters, wiring and markup are test-after.
- A value object's skeleton gets a no-op `__post_init__`, so a "refused" test goes red on
  `DID NOT RAISE`. `NotImplementedError` subclasses `RuntimeError`, so a skeleton passes
  `pytest.raises(RuntimeError)`.
- **A test edited in its GREEN commit is the failure this cycle prevents.** A RED found wrong (or
  contradicting the spec or its step order) is corrected in **its own commit**. A soft RED is
  **hardened right after GREEN**. A RED that can't be reproduced is recorded as such, never forced.
- A skeleton's signature change lands its mechanical test renames in the skeleton's commit.
  Re-seed before you remove. Remove skeleton scaffolding in its own commit.
- A production defect `qa` finds is held as `xfail(strict=True)`; the fix's commit removes the marker.
- **Every commit is green, and commit order is part of the plan.** A mapping never lands before its
  column. A constraint the mapping must satisfy, or any CHECK, lands with the mapping. The pre-commit
  hook tests the **working tree**, so stash the rest per commit. `TDD_RED=1` refuses a commit with
  no staged test. `commit-msg` rejects subjects over 72 characters only after the full suite.
- Domain tests have no I/O. Application/API tests use a **real Postgres** (`tailorcraft_test`,
  transactional rollback), never the dev DB. **No test calls Gemini.** Time comes from a fake
  `Clock`, whole-second, and race seeds use the same clock.
- **Isolation is checked per datastore.** The DB rollback does not reset Redis: `clear_redis`
  flushes the test Redis (db 3, boot-guarded against the live roles). **Run exactly one suite at a
  time.** Unrelated tests failing differently each run is contention. Run the suite twice to prove
  isolation.
- **A performance or liveness assertion is proven only by a mutation:** reintroduce the regression,
  watch it go red, and record both numbers. Time event-loop *turnaround*, not an in-process request.
  Pick the statistic for the workload.
- Inject a fault **below** the floor you test (patch the library call, not the adapter).
  `AsyncSession.get` reaches the sync `Session.execute`.
- Harness traps:
  - Races use `concurrent_app` (a real session per request), staged at the moment the spec names,
    with the staging seam surviving the fix.
  - Seeds on the test session **commit**.
  - A Core `UPDATE` needs `expire_all()`. A refused request expires loaded instances, so read
    values into locals first.
  - An idle-in-transaction backend reports `wait_event_type = 'Client'`.
  - Seed data goes through the domain grammars.
- Fakes: a fake needs the isolation of the thing it fakes and the **real response shape** (build
  it from a real response, pin it). Widening a port's return breaks monkeypatched stubs, so grep and
  fix them first.
- Structural rules: every domain `Protocol` is bound or exempted (`test_port_bindings.py`). "Every
  table with X" is discovered from the catalogue. "At head" is `get_current_head()`, never a pinned
  revision. A router-level dependency duplicated by a handler param is an equivalent mutant, so pin
  `router.dependencies`. **Check that a gate exists before a plan leans on it.**
- `make test file=<one integration file>` can fail at collection, so use `k=`. `make web.test`
  ignores `file=`. pytest in the container can't see repo-root files, so compose/nginx checks live
  in `scripts/git-hooks/`.
- `EXPLAIN` over one user's rows correctly seq-scans, so seed other users first. List performance
  budgets are measured on the **production bundle in a real browser**. Verify a harness gesture
  (Playwright `dragTo`) before blaming the app.
- **A test encodes what the code *should* do, never what it was observed doing.** When an AC and
  the code disagree, fix one on purpose and say which won.
- Known flakes (pass on re-run): AC-20's login event-loop fraction, a 401 on
  `…[control_character]`, the first admin Vitest test under load. If one recurs, it's a finding.

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
- **The mail provider sees the address** and a plain-text message carrying at most a one-time link
  (2.5, ADR-0026): no CV, no name, no HTML, tracking off. A plaintext token exists in one worker's
  memory and one mail, never in the broker, a log line or a row (only its SHA-256 is stored).
- **Guest data lives ≤ 24 h** and a scheduled job enforces it (FR-6,
  [ADR-0006](./docs/adr/0006-guest-retention-and-local-file-storage.md)). Registered users' data is
  never touched by that job, and the test proving it is written in the same slice as the job.
- **A guest session is not a weak login.** Owning a session id is not authority over an object that
  references it — check the link, in the use case, every time.
- **An ownership graph never crosses owners** (ADR-0022): a guest-owned row that pointed at a saved
  CV's file would let the 24-hour purge unlink a registered user's bytes. Since 2.4 an owner changes
  only through the claim (ADR-0025), which re-keys a session's **whole** graph in one transaction
  and never claims a working copy (a copy of a CV some account keeps, possibly another person's).
- **Guest work moves into an account only by an explicit offer that names it.** On a shared
  computer, the previous visitor's CV is in the browser for up to 24 hours; an automatic claim on
  login would file a stranger's CV in this account, kept until deleted.

## Infrastructure footguns (baked-in guards)

Full stories in [LESSONS.md](./LESSONS.md) and [docs/infrastructure.md](./docs/infrastructure.md).

**Proxy, network, compose**
- Pin Traefik to its network (`--providers.docker.network=traefik`), or you get a silent 504.
- **Docker bypasses UFW.** Datastores publish no ports in prod; dev binds `127.0.0.1`
  (pre-commit hook).
- Use named volumes only. Redis always has a password.
- **`TRUSTED_PROXY_HOPS` = proxies we run:** 2 on the box (Traefik + nginx), 1 in dev. nginx never
  runs `realip`.
- nginx `proxy_set_header` inheritance is all-or-nothing, so repeat the headers in any block that
  sets one.
- **A single-file bind mount is pinned to the inode.** The deploy force-recreates nginx and checks
  the *loaded* config.
- Under `pipefail`, never pipe into `grep -q`/`head`. Capture first, then grep a here-string.
- `env_file:` outranks the image `ENV`. The dev override pins `APP_ENV`, `PUBLIC_BASE_URL` and
  `MAIL_*` in `environment:`. `docker compose restart` ignores `.env` changes, so use
  `up -d api worker beat`.
- After a dependency lands, run `up -d --build api worker beat`. `make deps` must sync **api, worker
  and beat**.
- Every app container (`api`, `worker`, `beat`) is in the deploy's pull list and image check.
  Worker and beat are **stopped before the new API starts** and through the migration. Confirm a
  release-order claim by reading `deploy.yml`.
- A daemon writing to a relative path writes into the source tree (beat's schedule lives on a
  volume). The venv is at `/opt/venv`.
- `make db.dump` is not a backup: back up the uploads volume too. The uploads volume is shared by
  api and worker.
- `NODE_ENV=development` leaks into `vite build` (`make web.build` forces production). Use
  `tsc -b`, never a bare `tsc --noEmit`. Vite inlines assets under 4 KB (`.webp` is excluded).
- `tailorcraft-api:local` is the dev image. Measure on `docker build --target production`.

**Celery / Redis**
- **`/health/ready` probes Postgres, Redis and Celery**; beat is invisible to it.
- `task_acks_late` does not bring a lost task back. The stale-run sweep on beat recovers stuck runs.
- **kombu never removes a queue binding.** The healthy state is four members in
  `_kombu.binding.celery` (broker db `/1`). A fifth is stale, so `SREM` it. A local mutation of
  `task_queues` re-poisons the dev broker.
- **Every API `send_task` passes `ignore_result=True`.** Otherwise `AsyncResult.__del__` wedges the
  loop.

**Startup and secrets**
- The production `CMD` pre-flights `check-settings` (eleven refusals) before `exec uvicorn`. Run
  directly, uvicorn respawns a refusing import forever. The box's `.env` must carry `MAIL_*`.
- `JWT_SIGNING_KEY`'s placeholder is 33 bytes and is refused by name. Never use
  `Field(min_length)`: it echoes the value.
- A dev box with a real `GEMINI_API_KEY` spends money from the UI and from `make eval`.

**Async and CPU**
- **Any synchronous CPU-bound call on the loop blocks every user**, including "not real work" like
  sniffing a DOCX. Dropping `to_thread` from a sync call makes `wait_for` unable to cancel it, and
  the process hangs.
- pytest-asyncio's fixture and test loop scopes are both `session`.
- Keep `TC001`–`TC003` disabled: FastAPI/Pydantic/SQLAlchemy resolve annotations at runtime.

**SQLAlchemy / Postgres**
- A mapped class needs an explicit (empty) `__init__`.
- **A failed flush expires the whole identity map.** Read ids into locals first, use a SAVEPOINT per
  write in a batch, and `expunge` before `begin_nested()` (its entry flush skips version checks).
- **`NOT EXISTS` in a `DELETE` can't see an uncommitted `INSERT`.** Use `FOR UPDATE`, then a
  separate `DELETE`. The inserter takes `FOR KEY SHARE` **after** its `INSERT`.
- `with_for_update(key_share=True)` alone is `FOR NO KEY UPDATE`; `FOR KEY SHARE` also needs
  `read=True`. Compile and read the SQL.
- Take the strongest lock first: `FOR SHARE` then `FOR UPDATE` on one row deadlocks two callers.
  The user row is locked before any reset.
- A `SET` on a pooled connection does not survive a commit. Pin the connection and commit the
  `SET`. A hanging test is worse than a failing one.
- Alembic runs a revision in one transaction, so `NOT VALID` → `VALIDATE` staging releases no lock.
  Split the revision if a table grows.
- Alembic's `fileConfig` must pass `disable_existing_loggers=False`.
- PostgreSQL never frees dropped-column slots. `_migrated` recreates `public` per session (guarded
  to `*_test`).
- `get_settings()` under `APP_ENV=test` still returns the **dev** `database_url`. Anything that
  deletes names `test_database_url` and asserts `_test`.
- **A failed write leaks data three ways** (SQLAlchemy params, the driver message, the `from`
  chain). The `handle_error` listener withholds all three and copies `constraint_name`, which you
  read with `database.violated_constraint(exc)`, never by parsing. Postgres logs `terse`.

**FastAPI / HTTP**
- **A dependency's teardown runs after the response is sent** (0.141). A response that must reflect
  a write commits inside the handler.
- `app.routes` is not flattened on `include_router`, and `_IncludedRouter` serves copies. Walkers
  descend; spies wrap `route.endpoint`.
- A removed route whose path still matches another answers **405**, not 404.
- Two tokens minted in the same second are identical, so never key on token identity.
- httpx stores a `Secure` cookie over `http://` and never sends it back. Test clients use
  `https://testserver`.
- `urlsafe_b64decode` drops bad characters, so use `b64decode(…, altchars=b"-_", validate=True)`
  and parse digits only.
- The WHATWG parser strips tab/CR/LF in URLs: `safeNext` refuses any control character.

**Privacy plumbing**
- Sentry: `include_local_variables=False` (separate from `send_default_pii`). `strip_query_strings`
  runs as `before_send` **and** `before_send_transaction`.
- **Access logs drop query strings** (`StripAccessLogQuery` on uvicorn, a `noquery` nginx format).
  nginx `error_log` still logs them (accepted). Traefik is unchecked.

**Libraries**
- WeasyPrint: it needs Pango/Cairo/HarfBuzz/fonts in the Dockerfile **and** CI. A plain-function
  `url_fetcher` needs `_fail_on_errors`. `FatalURLFetchingError` is a `BaseException`.
  `li::marker { content }` crashes, so use `list-style-type: "…"`. Sanitize the fragment, then wrap.
- markdown-it drops a link without an `href`, leaving literal text.

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

When behaviour changes, update: this file (if a convention or command changed; keep it to one line
per rule, with the long form in [LESSONS.md](./LESSONS.md)), the slice's entry in
[HISTORY.md](./HISTORY.md), the relevant ADR (if a decision changed), and
[FORboehpyk.md](./FORboehpyk.md), the running plain-language project story, per the owner's
standing rule.
