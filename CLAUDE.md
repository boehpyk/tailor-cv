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

> **Status: five slices shipped; slice 1.6 built, verified (`/verify` PASS), rehearsed on real data
> and switched on, 2026-09-22.** `GUEST_PURGE_ENABLED=true`; `/health/ready` reads
> `scheduled: true`, `stale: false`, `overdue: 0`. Every task in the slice is closed.
> Phase 1 is under way. The architecture now carries a paid external call, a worker, three scheduled
> jobs, an unauthenticated *write* to a PII row on a timer, a stranger's CV rendered into HTML and
> written to disk as a file, and — new in 1.6 — **the first `DELETE` in the codebase, irreversible
> in two systems at once.**
>
> - **1.1 `intake-base-cv-upload`** (PR #1) — upload a base CV, sniffed by its bytes, extracted in a
>   worker thread, owned by a guest session.
> - **1.2 `posting-job-description-intake`** (PR #2) — paste a job description or hand over a link,
>   fetched behind a guarded egress (ADR-0012) with FR-2's paste fallback as an action.
> - **1.3 `tailoring-generate-documents`** (PR #4) — one button returns a tailored CV and a cover
>   letter: queued, executed by a Celery worker behind `LlmPort` (Gemini), polled by the client;
>   every outcome that spent money is a row (ADR-0014); a stale-run sweep on beat.
> - **1.4 `workspace-progress-and-editor`** (PR #5) — the tabbed workspace, the progress stepper,
>   React Router, and a TipTap editor over both documents with debounced autosave. The edit is a
>   revision on the run (ADR-0015); one version column refuses a stale edit **and** closes 1.3's
>   concurrent duplicate delivery. `/verify` took four rounds and ended by re-modelling the autosave
>   hook as one pure state machine.
> - **1.5 `export-multi-format-download`** (PR #7, merged) — four formats. `md` and `txt` are a
>   `GET` on a representation of the document and leave **no row**; `pdf` and `docx` are an
>   `ExportJob`, a Celery task on a third queue, a file on the uploads volume and a polled client
>   (**ADR-0016**). The pipeline is Markdown → tokens (`html=False`) → grammar normalization →
>   {plain text | DOCX | HTML → `nh3` → WeasyPrint}, with a `url_fetcher` that refuses every URL
>   (**ADR-0017**). **1314 backend and 504 frontend tests**, green twice. Measured: inline p95
>   **11 ms** (budget 500 ms), `POST` → `ready` p95 **0.17 s** over 40 real renders (budget 10 s),
>   corpus **28/28** across four formats. `/verify` took **four rounds** and found **three MAJORs**,
>   all of them in code a green suite of 1293 tests was happy with — see below.
>
> - **1.6 `retention-guest-purge`** (branch open, `/verify` **PASS**, **not yet rehearsed**) — the
>   purge, the orphan sweep, the CLI, the beat entry and a Retention block in the status panel.
>   **1456 backend and 532 frontend tests**, green twice. `retention` is the first context with **no aggregate** — a policy,
>   two use cases, two ports (**ADR-0018**) — and `/health/ready` gained `jobs.guest_purge`, the
>   first thing it reports as a *fact* rather than as readiness (**ADR-0019**). **No migration**, and
>   that is proven by reading `pg_constraint`/`pg_index` rather than trusting the comments that
>   promised it. Measured: a 100-session purge (300 files) in **0.35 s** against a 10 s budget; the
>   `overdue` probe **2.1 ms p95** against 20 ms.
>
> **`/verify` took three rounds and found four gaps a green suite of 1423 was happy with — and all
> four were the same *kind* of gap: something the spec promised that no test asserted.**
> - **The beat entry and its task had no test at all.** AC-25…AC-29 were entirely unasserted,
>   including `GUEST_PURGE_ENABLED` — the single control keeping the first `DELETE` off a schedule
>   before the rehearsal. Nothing proved the flag worked.
> - **AC-38's privacy test did not exist**, and had never been given a task. The spec's own Privacy
>   section said the "never logged" list was *"asserted by AC-38's planted-marker test, not by
>   intention"*. That sentence was false for the whole slice. It is true now: markers planted in
>   every PII field, plus the real storage keys and joined paths, over a real purge **and** a real
>   orphan sweep on a real filesystem.
> - **R-3 and R-4 named two log events that existed nowhere in `api/src/`**, and the handler was a
>   bare `except Exception:` that never bound the exception — so `error_type` was unrecoverable *in
>   principle*. The consequence is this slice's own thesis one level down: `_purge_batches` breaks on
>   `sessions_deleted == 0`, so a permanently-refused `DELETE` is retried hourly **for ever**, the run
>   still **exits 0**, `overdue` sits above zero, and nothing names the session or the reason.
>   Fixed by **returning** the failures (`PurgeReport` grew two tuples, **AC-4 amended**) rather than
>   logging from `application/` — which keeps the layer silent *and* keeps every emitted record inside
>   AC-38's field of view.
> - **The symlink seam**, below.
>
> **The `.part`/symlink pattern appeared three times in one slice, all in the file store, all
> invisible to a recording fake.** One lesson, worth learning once: *the thing named is not always the
> thing acted on.*
> 1. The partial that survived while a live file beside it died (T18b, below).
> 2. **A symlink whose target died while the link survived.** The scanner passes `follow_symlinks=False`
>    everywhere and is correct; `_resolve_contained` called `.resolve()`, which **follows**. Opposite
>    policies either side of one seam. The orphan sweep is the **first caller in the codebase that
>    deletes by a name it discovered on disk**, which is what turned a pre-existing `resolve()` into a
>    deletion primitive: a link at a `FileRef`-shaped key is reported by the scanner, clears the
>    cross-check (the *link's* key is in no row), and is "reclaimed" — destroying a live, referenced
>    file belonging to a **different session** while the orphan survives.
> 3. **`_put_sync` opened `<key>.part` with a plain `open()`**, which follows a link — so bytes landed
>    on the target and `os.replace`, which renames the link rather than following it, then installed
>    **the link itself** at the real key. One upload overwriting an arbitrary file on the volume, and
>    that key a symlink from then on. Found by following the fix rather than closing the ticket.
>
> **The fix is uniform and is now stated once in the module docstring** so nobody re-derives which
> path was which: `put` and `get` open `O_NOFOLLOW` through one `_nofollow_opener` passed to
> `open()`'s `opener=` hook and work on the **descriptor**; `delete`/`delete_partial` call `unlink`,
> which removes a link and never its target; `_resolve_contained` resolves the **parent** and never
> the basename, refusing a final-component link — but it is a **check-then-use**, so it is the outer
> lock and never the only one. `os.fchmod(fd, …)` replaced `os.chmod(path, …)`: a descriptor cannot be
> re-pointed between the open and the chmod, and `fchmod` is immune to the umask that `O_CREAT`'s mode
> argument is not. The opener also closed a descriptor leak **by construction** — `open()` owns the fd
> it returns — which removed code instead of adding a handler.
>
> **What 1.6 found that no passing test could:**
> - **A test that samples an in-process route cannot see a blocked event loop** — found at T47, by
>   mutation-testing AC-42's brand-new loop-liveness test against the regression it was written for.
>   It passed. `/health/live` does no I/O and the test client is an `httpx.ASGITransport`, so the
>   request resolves through nested `await`s that **never reach a real suspension point**: the loop
>   cannot switch to a blocking worker in the middle of one, and a request that starts while the loop
>   is free finishes while the loop is free, at full speed. **Timing the request measures the one
>   window in which the loop is by construction not blocked.** Time the *turnaround* instead —
>   `sleep(pace) + GET`, minus the pace — and the same mutation goes from a sub-millisecond p50 to
>   **1264 ms**. The mutated run had been taking 27 s instead of 3 s the whole time; wall-clock knew,
>   the assertion did not. **AC-10's test had the identical defect and had never been able to fail
>   either**; AC-11's copy is unproven in both directions. A second corollary, because it decides the
>   sample floor: **blocking suppresses sampling**, so a blocked loop under-represents itself in the
>   set being summarised and a p50 is only sensitive when the blocked fraction is over half.
> - **A `.part` file was never deleted, and a live file beside it was.** `FileRef`'s grammar ends
>   `\.(pdf|docx|txt)$`, so a partial *cannot be a `FileRef`* and travels as base-ref-plus-flag —
>   and the sweep called plain `delete(ref)` for both, which unlinks the **final** key with
>   `missing_ok=True`. So the `.part` survived and was reported `reclaimed`; and where a live file
>   sat at that key it was **deleted**, with the reference cross-check structurally unable to save it
>   because partials are excluded from it by design. Reachable: `put` #1 succeeds, the task is
>   redelivered, `put` #2 dies before `os.replace`. **`FileStorePort.delete_partial` is the fix — a
>   second method, not a boolean**, because a flag threaded from a variable is the shape that caused
>   it. Found only because a test was rewritten to drive a **real filesystem** instead of a recording
>   fake: R-36's existing test passed throughout, because a fake has no filesystem and cannot model
>   the difference between `K` and `K.part`.
> - **`NotImplementedError` subclasses `RuntimeError`**, so a red-first test asserting "an unexpected
>   exception propagates" with `pytest.raises(RuntimeError)` passes **vacuously** against a skeleton.
> - **A `SET` on a pooled connection does not survive a commit** — and it hung CI for twenty minutes.
>   `SET lock_timeout` was issued once, then a purge that **commits per session** ran; `QueuePool`
>   hands back a different physical connection after each transaction, so the timeout was never in
>   force and a deliberately-locked row waited unbounded. Pin one connection and bind the
>   sessionmaker to *it*, and **commit the `SET`** — a bare `SET` is itself transactional.
>   **A test that hangs is worse than one that fails**: CI sits on it until timeout and the failure
>   names nothing.
> - **`/health/ready` costs ~2.1 s against its own 300 ms budget, and has since Phase 0.**
>   `probe_celery` is **2128 ms** of it; the new retention probe is **1.8 ms**.
>   `control.ping(timeout=2.0)` is a broadcast with **no reply limit**, so it waits the whole window
>   however fast the worker answers — `ping(timeout=2.0, limit=1)` returns the same reply in
>   **4.0 ms**. Not changed in this slice: it would stop `detail` reporting the worker count, which
>   is a trade for the owner to make. Owner: `devops`; trigger: before anything relies on this
>   endpoint's timing.
> > - **The dev uploads volume held 753 orphaned files out of 827** — residue from 1.1–1.5. The
>   arithmetic reconciled exactly in both directions, which is what made it residue rather than a
>   cross-check that was failing to match. **Cleared in the 2026-09-22 rehearsal**: the purge took
>   the 74 referenced keys, the sweep reclaimed the remaining 753 with 0 failures, and the volume is
>   now empty. `overdue` is 0 and the dev database holds no guest data.
> - **`tailorcraft_test` carried a committed, already-expired session**, so `count_expired` returned
>   1 on an "empty" database. Scope assertions to ids the test created; an absolute count is one
>   stray row from lying.
>
> **Carried out of 1.6, each with an owner and a trigger:**
> - **The schedule is ON as of 2026-09-22 16:04 UTC.** The rehearsal ran in order first — both
>   backup halves, dry run (11 sessions / 74 keys), `limit=5` verified at exactly −5 with a
>   row-and-file spot-check, full purge to `overdue` 0, orphan sweep 753/0, volume empty. **Beat's own tick fired
>   at 17:04:53**, exactly 3600 s after beat started, and the worker ran it in 35 ms (`examined=0`,
>   `last_outcome: ok`, `stale: false`): **a run that deletes nothing is still visible**, which is the
>   heartbeat's whole job and the reason the backlog — not the log line — is the signal to trust.
>   **`docker compose restart` does not pick up an `.env` change** — `env_file:` is read at container
>   *create*. Use `up -d api worker beat`, and note `api` belongs in that list: it serves `scheduled`
>   on `/health/ready`, so a beat-only change leaves the UI saying "off" while the job runs.
>   **`limit=50` in the runbook was wrong for a backlog of 11** and now reads "smaller than the
>   backlog you just read": a bite bigger than the backlog silently skips the safeguard.
> - **AC-17's uploads/exports split was dropped**, amended on purpose: a `FileRef` is an opaque key
>   with **one grammar shared by both kinds**, the extension lies (`.pdf` is both), and widening
>   `ExpiringGuestSession` would contradict AC-4, whose third field's *type is the privacy control*.
> - **R-38's "counted `failed`" is unreachable** through the port as designed, and the port was
>   deliberately not widened: `failed` means "we tried to reclaim this and could not", and folding
>   "we could not even look here" into it would make one number mean two things.
>
> **Every gate was verified by running it**: Ruff, mypy `--strict`, import-linter (3 contracts, now
> with `weasyprint`, `nh3`, `markdown_it` and `docx` on both forbidden lists), pytest, `tsc -b`,
> ESLint, Prettier, Vitest, `vite build`. `make eval` measures prompt quality against the real API.
> It is not a test, and it costs money.
>
> **What 1.5 found that no unit test could:**
> - **A plain-function `url_fetcher` aborts the whole render in WeasyPrint 70.** The library reads
>   `url_fetcher._fail_on_errors` *inside its own `except`* to choose between a per-resource failure
>   and a fatal one; a function has no such attribute, so our refusal came back out as an
>   `AttributeError` and the `except Exception` floor would have recorded `render_error` saying
>   nothing. Every fetcher now passes through a wrapper carrying that attribute.
> - **`weasyprint.urls.FatalURLFetchingError` subclasses `BaseException`, not `Exception`** — a hole
>   in every floor, found by walking the installed package rather than reading a changelog. Caught by
>   name; the floor stays `Exception`, so `CancelledError` still cancels.
> - **`sanitize_html(render_html(...))` deletes the document shell and keeps the title's text**,
>   putting a stray "Tailored CV" line above the name in every PDF. `html`/`head`/`title`/`body` are
>   not on the allow-list, correctly. Sanitize the fragment, **then** wrap.
> - **`beat` crash-loops on the production image.** `/var/lib/tailorcraft/state` did not exist in the
>   image, so Docker created the volume's mount point **root-owned**, and `beat` runs unprivileged in
>   production but not in dev. Invisible in development by construction.
> - **markdown-it does not hand back a link without an `href`** — a refused scheme fails the whole
>   rule and leaves the literal `[text](javascript:…)` as one text token.
>
> **What `/verify` found on top of that, in code 1293 green tests were happy with:**
> - **The refused-link helper deleted the author's own characters.** `Negotiated salary range
>   [100k](150k)` came out of every PDF, DOCX and TXT as `…range 100k`. One predicate was being asked
>   two different questions: `_allow_three_schemes` is the *parser's* gate ("may this become an
>   `href`?"), while the helper runs over text markdown-it has **already refused** and must ask "was
>   this ever a URL?". A destination with no scheme was never a URL attempt. **`_is_refused_url` is
>   the second question**, and the corpus now carries a `[label](plain-word)` fixture — its absence is
>   why AC-47's 28/28 could not see this.
>   **A one-character scheme is a drive letter, not a scheme** (`urlsplit("C:/…").scheme == "c"`), so
>   the guard is `len(scheme) > 1`. The argument for that is worth more than the line: **the harm is
>   asymmetric.** Over-stripping deletes text and the reader never learns anything went; under-
>   stripping shows inert text the parser already refused, with *zero* security cost, because by then
>   there is no `href`. Put a heuristic's error on the side that shows too much.
> - **An error state whose only affordance reproduced the error.** A 410 `export_file_gone` does not
>   change the job row — the download endpoint writes nothing, correctly — so a control that asked
>   the *row* what a click meant went on answering "download this ready file" for ever, under copy
>   reading *"no longer available — Export again"*. The click's meaning now comes from the **view**,
>   which deletes the second derivation that was the actual cause.
> - **Five failure-contract rows reached nobody.** `requestExport.isError` was read nowhere, so X-14,
>   X-18, X-19, X-21 and X-22 all rendered silence. Three of them **commit no row** (ADR-0014 §2), so
>   the poll could never surface them either — and the obvious response to silence is to click again,
>   which for the 429 and the session cap is exactly what the limit exists to stop.
> - **A test that could no longer fail.** Deleting the unsanitized `render_html` was right; re-pointing
>   its three assertions at the sanitized composition was right for two of them and made the third
>   vacuous, because `sanitize_html` emits only the allow-list *by construction*. Break the emitter's
>   heading clamp and all 13 tests stayed green. **The emitter's promise is "for any stream anybody
>   hands it", so testing it requires a stream the pipeline would never produce** — the fixture needs
>   a raw `h4` that `normalize_to_grammar` would have clamped away.
>
> **Carried out of 1.5, each with an owner and a trigger:**
> - **Startup refusals never exit under `uvicorn --workers N`** — now **three** guards (the API key,
>   the tailoring stale window, the export stale window). Owner: `devops`, before the deploy SSH
>   secrets are set.
> - **AC-37 forces an a11y regression** (the ticking count cannot hide in an `aria-hidden` span), and
>   **AC-42's 401 copy ships without its link home** — the latter still blocked, because the view's
>   union was **re-pinned** by `toEqual` in the same commit that widened it. Both recorded in the spec.
> - **X-46's stale-download log line is unbuilt** — deliberately. The reviewer's recommendation is to
>   strike the row's "Logged" cell instead: `current` is already reported on every one-second poll, so
>   a log line would be a *derived copy* that can disagree with the resource. If the operational
>   question is ever genuinely wanted, a domain event is the vehicle, not a widened use-case return.
> - **`test_document_renderer.py` writes a float into an `int` settings field** via `model_copy`,
>   which does not validate. The timeout branch is proved against a value the type forbids. A settings
>   field a test must lie about is a hint the field wants to be a float.
> - **A timing test's precondition can scale inversely with machine speed — AC-11's did, and it went
>   red in CI.** The loop-liveness test asserts a **p50** of `/health/live` latency while 20 CPU-bound
>   renders run, behind a floor of "at least 20 samples". The flake was **bidirectional**: a loaded
>   host drifts p50 past its bound, and a *fast* host finishes the fixed 240-render batch inside 16
>   sampler ticks and trips the floor instead. GitHub's runner hit the second — with sampled latencies
>   of **0.5 ms**, i.e. the property under test holding comfortably while its scaffolding failed.
>   The floor was right; **coupling it to how long the work happened to take was not**. The renders now
>   run until the sampler signals it has its 20 samples, so the batch duration is an *output* rather
>   than an assumption, and the count is a self-check on an invariant instead of a race.
>   **`tests/integration/adapters/test_posting_fetcher_event_loop.py` has the identical latent shape**
>   — the same `len(latencies) >= 20` floor, coupled to a four-fetch batch. It has not flaked, probably
>   because network I/O plus `trafilatura` is slow enough; fix it the same way if it ever does.
> - **Dropping `asyncio.to_thread` from the renderer does not slow the loop — it hangs the process,
>   uninterruptibly.** Measured while mutation-testing the above. A coroutine wrapping a synchronous
>   call has **no internal suspension point**, so there is no `await` boundary for a `CancelledError`
>   to land on: `asyncio.wait_for` cannot preempt it, the timeout never fires, and one core sits at
>   99.9% until the process is killed from outside the container. This is worth knowing twice over.
>   It is a *stronger* proof that the loop was blocked than an elevated p50 would be — and it means a
>   `wait_for` hang-guard around such a batch protects only against a **partial** regression (one that
>   still yields occasionally), never against the total case. Closing that properly needs an OS-thread
>   watchdog with a hard `os._exit`; judged disproportionate, and recorded rather than assumed away.
> - **`markdown_it` is an unsilenced vendor logger sitting on the raw CV.** Measured: it does not leak
>   today (counts, not text). Same "clean today" argument as the `httpcore` note.
> - **The production image has no `pytest`**, so AC-50's in-image render check needs a per-invocation
>   install. A `test` build target on `production` is the call; nobody has made it.
> - **Still open from 1.4:** very long CVs can exceed the per-attempt LLM timeout; beat is invisible
>   to `/health/ready`; the editor is not lazy-loaded (530 kB of TipTap on a page where nobody edits).
>
> **CI on GitHub is verified** — `api` and `web` both pass on `main`, and the deploy's **build** job
> pushes images to GHCR.
>
> **The deploy path is still unproven, but the gate is now real.** There is no VDS, so `deploy` would
> fail at the SSH sync — expected. **Corrected 2026-09-19, by reading the API rather than this file:**
> the `production` environment now carries **two protection rules** — a `required_reviewers` rule
> naming the repository owner, and a `branch_policy` limiting deployments to protected branches. The
> "manual-gated deploy" this file and `docs/cicd.md` describe therefore **does** exist. An earlier
> revision of this paragraph said the environment had zero protection rules and that a missing
> `SSH_KEY` was the only thing stopping a release; that was true when written and **was repeated for
> a whole slice after it stopped being true**. The repo also currently holds **no secrets at all**
> (`gh secret list` is empty), so a merge to `main` runs `build` and then *waits* for a human.
>
> The standing rule that produced the original warning still holds and is worth keeping: **a control
> nobody has verified is a belief, not a control.** Check it with
> `gh api repos/<owner>/<repo>/environments/production` before trusting either this file or the
> deploy docs — including this sentence.
>
> `docs/adr/**` and `docs/constitution.md` are tracked; the PRD, the specs and the infra notes stay
> local. **The repository is public**, so anything added to `docs/adr/` is published the moment it
> lands.
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
curl -s localhost:8080/health/ready | jq .jobs.guest_purge   # the backlog — the signal to trust

# Orphans: files with no row (the crash window's survivors). Operator-run only, never on beat.
# Fails CLOSED — if the database cross-check cannot run, it deletes nothing and exits 1.
python -m tailorcraft.cli purge-guests --orphans --dry-run
# Exit codes: 0 success (including deleting nothing) · 1 failed · 2 usage · 3 THE LOCK WAS HELD.
# 3 is the point: a run that did nothing because another holds the lock must not exit 0.

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
  purge lock survive between tests and must be cleared in the fixture.
  **`clear_redis` flushed the *dev* Redis until 2026-09-22 — the suite broke the running system's
  purge status on every run, and nothing said so.** The `settings` fixture swapped `database_url` and
  `upload_dir` and **not** `redis_url`, so `flushdb()` landed on the box's real Redis while the
  fixture's docstring called it "the test Redis". Through the pre-commit hook that meant **every
  commit wiped the purge heartbeat**, which with the schedule on reads as `stale: true` on a perfectly
  healthy system — AC-33's rule working correctly on a premise the suite invented. It also dropped the
  kombu bindings and the rate-limiter counters.
  **Fixed: `Settings.test_redis_url`**, derived from `redis_url` with the database swapped to 3 so the
  password is written down once, and — this is the actual fix — **a boot guard that refuses a test
  Redis resolving to the same `(host, port, db)` as the cache, the broker or the result backend**.
  Compared on identity rather than on the URL string, because `redis://redis:6379/0` and
  `redis://:secret@redis:6379/0` are one database and a string comparison waves the dangerous case
  through. A correct URL fixes today; the guard is what makes tomorrow's `/0`-instead-of-`/3` loud.
  Not a narrower flush — that would reintroduce the leftover-lock hazard `clear_redis` exists for.
  **The general lesson is the transferable part: test isolation is a property you check per
  datastore.** Postgres was isolated, the filesystem was isolated, and Redis looked isolated because a
  docstring said so. Proof it holds is a sentinel key and a live heartbeat surviving a full run, not a
  green suite. The cheap proof you got it
  right is to **run the suite twice in a row** — a second run that fails is the classic symptom. A
  leftover *lock* is the dangerous one: the job then does nothing, logs "skipped", and exits 0, so a
  test asserting a successful run passes against a run that never happened.
  **Run exactly one suite at a time — the suite is not safe to run concurrently, and nothing in it
  will tell you so.** `clear_redis` calls `flushdb()`, which is **global**: a second pytest process
  (another terminal, an agent running gates, `pytest -n`) sharing this Redis will delete the first
  run's rate-limiter counters *mid-test*. The limiters then **fail open**, so a third request that
  should be `429` returns `201`/`202`, and the knock-on assertions fail with unrelated `409`s.
  The signature is unmistakable once you know it and baffling until then: **a different set of
  unrelated tests fails on each run, and every one of them passes in isolation.** That is shared-state
  contention, never a defect in the code under test — it cost two separate sessions real time during
  1.6's `/verify`. The same applies to `tailorcraft_test`. Judging suite health from a *subset* run is
  the milder version of the same error: a subset leaves rate-limiter keys uncleared, and an
  interrupted run can leave the schema downgraded.
- **A performance or liveness assertion is a claim about a mechanism, and the only proof is a
  mutation.** Both of this codebase's event-loop tests were green against the exact defect they
  named (T47) — not because the code was fine, but because the *measurement* could not observe it.
  Re-introduce the regression, watch the assertion go red, restore the source byte-exact, and write
  both numbers into the test. An assertion that has never been observed failing is a docblock.
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
- **`task_acks_late=True` does not mean "a lost task comes back".** Celery redelivers only when the
  worker's *main* process dies holding the message, and on Redis only after `visibility_timeout`. A
  task that raises or hits the hard time limit is acked (`task_acks_on_failure_or_timeout=True`), and
  so is one whose pool child is OOM-killed. `task_reject_on_worker_lost` is left unset on purpose,
  because a prompt redelivery would find the run `running` and skip it. Slice 1.3's `/verify` found
  runs stuck `running` for ever this way. The stale-run sweep on beat recovers them now, and **beat is
  not a worker**, so `/health/ready` cannot see it stop.
- **Fixing a Celery queue declaration does not undo the old one.** kombu *adds* a binding each time
  a queue is declared and never removes one, and Redis keeps them. Slice 1.3 first declared both queues
  without a routing key, so both bound key `celery`. One publish on that key would have reached both
  queues: two deliveries of one run, and two paid calls. Correcting the keys and restarting left the
  stale binding in place until someone ran `SREM _kombu.binding.celery` (the exact member is in
  `infrastructure/tasks/app.py`'s comment). **A local mutation test of a queue declaration re-poisons
  the dev broker too**, because `watchmedo` restarts the worker on the edited file. At `/verify` that
  happened to an agent that already knew about this trap. After any change to `task_queues`, compare
  the broker's binding sets against the new declarations — but read them correctly: kombu names a
  binding set after the **exchange**, not the queue, so with all queues on the one default `celery`
  exchange (`task_default_exchange`, unchanged through slice 1.5's third queue), the healthy state is
  **three members in the single set `_kombu.binding.celery`** — `_kombu.binding.tailoring` and
  `_kombu.binding.export` do not exist at all, and finding "one member each in three sets" describes a
  broker with three exchanges, not this one. A stale binding is a **fourth** member of
  `_kombu.binding.celery`, which is exactly the shape the `SREM` example above deletes. Also:
  `CELERY_BROKER_URL` is db `/1` — `redis-cli` without `-n 1` reads db `0`, where every set is empty,
  which reads as a clean broker for the wrong reason.
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
  suspecting the network. Slice 1.3's `/verify` added a second refusal that behaves the same way.
  `create_celery` refuses a `TAILORING_STALE_AFTER_SECONDS` at or below the 180 s hard time limit.
  `infrastructure/api/main.py` imports the Celery app, so a bad value there also leaves the API
  respawning for ever, while the worker and beat exit loudly.
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
  covers only one.**
  1. SQLAlchemy's `[parameters: …]` line.
  2. The driver's own message. asyncpg quotes values, and PostgreSQL's
     `DETAIL: Failing row contains (…)` is the whole row.
  3. The `raise … from` chain, which Celery and Sentry render.

  `include_local_variables=False` reaches none of them, and the worker lets a failed save escape on
  purpose (G-28). `persistence/database.py` keeps the flag and adds a per-engine `handle_error`
  listener that withholds the driver's message and the bound parameters and cuts the chain. There is no
  off switch. Postgres's **server log** holds a fourth copy, so `docker-compose.yml` pins
  `log_error_verbosity=terse` and `log_parameter_max_length_on_error=0`. A test that checks only
  `str(exc)` will pass a one-flag fix, so assert on `traceback.format_exception(exc)`.
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
- **A bare `tsc --noEmit` on a solution-style `tsconfig.json` type-checks zero files.** `web/tsconfig.json`
  is `files: []` plus two references; without `-b` (or `-p tsconfig.app.json`) tsc compiles nothing
  and exits 0. The Makefile, the `build` script and CI all ran that form for four slices, so the
  "types" gate was `vite build`'s transpile and nothing more. `tsc -b --noEmit` is the gate now, and
  the six errors it surfaced were fixed in the same commit. A gate that checks nothing also supplies
  confidence.
- **A failed flush expires the whole identity map, and it does so *inside* the flush.** SQLAlchemy
  rolls a failed flush back to the nearest transaction boundary; at the root that is
  `_restore_snapshot(dirty_only=False)` — every loaded instance is expired before your `except`
  runs, and the next attribute read on any of them is a lazy load, which on an `AsyncSession` is
  `MissingGreenlet`. Two consequences met in 1.4: read `run.id` into a local **before** the flush
  (after it, the read raised `PendingRollbackError` and a 409 became a 503), and a batch that keeps
  iterating loaded aggregates after one refused write must contain each write in a SAVEPOINT
  (`expunge` → `begin_nested()` → flush → commit; `begin_nested()` itself flushes on entry, so a
  dirty aggregate handed to it flushes *outside* the SAVEPOINT). A `rollback()` is a statement about
  the session, not about the one object that failed.
- **`get_settings()` under `APP_ENV=test` still returns the *dev* `database_url`.** Only
  `tests/conftest.py` swaps in `test_database_url`, by overriding Alembic's option and the engine
  fixture. A probe or cleanup script that builds its own engine from `settings.database_url` — even
  with `APP_ENV=test` — is pointed at the dev database, and one such script's `DELETE FROM
  tailoring_run; DELETE FROM identity_guest_session` emptied dev through the cascades during 1.4's
  verify. Anything that deletes must name `test_database_url` explicitly and assert the URL contains
  `_test` before the first statement.
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
