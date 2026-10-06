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

> **Status: eleven slices shipped (1.1–1.6, 2.1–2.5); Phase 2 is closed, its gate met on 2026-10-03; Phase 3
> has begun: slice 3.1 `tracking-application-board` is verified (reviewer PASS round 2, 2026-10-06;
> manual pass T33); its PR is pending.** Slice 2.1 was verified (two rounds, 2026-09-25), merged as PR #13
> and released to `cv.samolit.com` the same day** (deploy run 36124532227). The box's `.env` read
> `TRUSTED_PROXY_HOPS=1` on 2026-09-25 and **reads `2`** over SSH on 2026-09-26 (T31) — the fact is
> fixed; the footgun below stays. **Slice 2.2 `intake-saved-base-cvs` was verified (two review
> rounds plus a manual `:8080` pass, 2026-09-26), merged as PR #14 (`4c18557`) and released**
> (deploy run 36265111930; the box's api/worker/beat all read `4c18557` on 2026-09-30). **Slice 2.3
> `tailoring-application-history` was verified (three review rounds plus a real-browser pass and
> one real-Gemini run, 2026-09-30), merged as PR #15 (`0b01537`) and released the same day**
> (deploy run 36782086963). **Slice 2.4 `workspace-registration-cta` was verified on 2026-10-01
> (reviewer PASS in round 1, plus a real-Gemini manual pass), merged as PR #16 (`2427b67`) and
> released the same day** (deploy run 36920406580). **Slice 2.5 `identity-email-verification` was
> verified on 2026-10-03 (reviewer PASS in round 2, plus a manual `:8080` pass with Mailpit), merged
> as PR #17 (`eeec430`) and released the same day** (deploy run 37136396246). `check-settings` on
> the 2.5 image gates **deploy approval, not the merge** (the image exists only after the merge).
> Mail is delivered by Resend with `spf/dkim/dmarc=pass`, tracking off (ADR-0026's T44 amendment;
> retention not yet read). **PR #18 (`dbeb328`)** fixed the deploy so nginx is recreated every
> release and its *loaded* config verified: no nginx change had reached production since the first
> release (footgun below). **Phase 2's gate is met** (clause 1 closed by a canary account that
> crossed `03494836ce30` → `b1b518fe84b1`). Slice 1.6 was verified, rehearsed on real data, switched on and merged as
> PR #8, 2026-09-22: `GUEST_PURGE_ENABLED=true` in dev; `/health/ready` reads `scheduled: true`,
> `stale: false`, `overdue: 0`. **Phase 2 started with Phase 1's gate unrecorded** (OQ-7 — the
> roadmap says so; the owner records it met with evidence, or open with why). The architecture now carries a paid external call, a worker, three scheduled
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
> - **1.6 `retention-guest-purge`** (PR #8, merged; rehearsed and switched on) — the
>   purge, the orphan sweep, the CLI, the beat entry and a Retention block in the status panel.
>   **1456 backend and 532 frontend tests**, green twice. `retention` is the first context with **no aggregate** — a policy,
>   two use cases, two ports (**ADR-0018**) — and `/health/ready` gained `jobs.guest_purge`, the
>   first thing it reports as a *fact* rather than as readiness (**ADR-0019**). **No migration**, and
>   that is proven by reading `pg_constraint`/`pg_index` rather than trusting the comments that
>   promised it. Measured: a 100-session purge (300 files) in **0.35 s** against a 10 s budget; the
>   `overdue` probe **2.1 ms p95** against 20 ms.
>
> - **2.1 `identity-register-and-login`** (PR #13, **verified, merged, released**) — the first
>   registered principal and the first password. `User` and `Login` aggregates beside an untouched
>   `GuestSession` (its file is pinned by digest; no route depends on both resolvers). A `Login`
>   **is** a refresh-token family: generations, a **10 s race grace** answered 409
>   `refresh_in_progress`, retired hashes in an append-only lookup table, and **revocation is
>   `DELETE`** (**ADR-0020**). Passwords are argon2id (`m=65536,t=3,p=4` — constants, no setting)
>   on a **dedicated two-worker executor whose size is the memory cap**, verified against a decoy
>   for an unknown email (**ADR-0021**). Access tokens: HS256, 15 min, exactly five claims, judged
>   by the `Clock`. Refresh cookie `tc_refresh`: opaque, stored as SHA-256, `HttpOnly;
>   SameSite=Strict; Path=/api/auth`, host-only, rotated on every use, 30-day absolute lifetime.
>   The four cookie-touching endpoints check a trusted `Origin` **before** the limiter, the
>   database and the hasher; the login/register limiters **fail closed** and refresh has none, so
>   Redis down stops new logins and never logs anyone out. Registration **enumerates** (409) until
>   an email channel exists (ADR-0008 amendment (b), roadmap 2.5). React: `/login`, `/register`,
>   `/account`, a header block, one auth store read through `useSyncExternalStore`, a boot refresh
>   at module scope, a bearer interceptor that refreshes once. One expand-only migration, three
>   tables. **1818 backend and 647 frontend tests.** **AC-52 holds**: `git diff main --stat` over
>   `domain/tailoring`, `application/tailoring` and `infrastructure/llm` is empty — the prompt
>   cannot have gained an email or an account id.
>   Measured on the **production image** (T48): argon2 hash **51 ms** (budget 40–250 ms); `login`
>   p95 **69.9 ms** (300); `refresh` p95 **7.8 ms** (50); `me` p95 **4.2 ms** (30); unknown-email
>   vs wrong-password median Δ **0.64 %** (≤ 10 %, AC-28); RSS growth under 8 concurrent logins
>   **128.5 / 128.0 MiB** per worker against **160 MiB** — the budget closest to its limit. If the
>   box is tight, the executor drops to 1 before the parameters drop.
>   **Rotating `JWT_SIGNING_KEY` logs nobody out** — refresh tokens are rows, not signatures, so a
>   new key only retires 15-minute access tokens and the next silent refresh mints new ones. The
>   break-glass is `revoke-logins --all` (Commands). 2.1's account deletion (an operator deleting
>   the `identity_user` row, OQ-8) is **wrong from 2.2 on** — it orphans every saved file; use
>   `erase-account`. The footguns it hit are
>   filed under Conventions and Infrastructure footguns below, not here.
>   **2.1's `/verify` took two rounds.** Round 1's CRITICAL was not in 2.1's code at all: the box's
>   `TRUSTED_PROXY_HOPS=1` had keyed every IP limiter on Traefik since Phase 1, and 2.1's fail-closed
>   limiters would have turned that into a site-wide login lockout. Found by reading the proxy chain,
>   never by a test — every test hard-codes its `X-Forwarded-For`. Round 1 also fixed a refresh that
>   re-authenticated a tab after logout (a sign-out counter in the store, not the reducer) and a
>   deleted user's endless Retry. **Carried, filed in the PR:** `SignOutReason` is never rendered;
>   logout lands on `/login?next=/account`; a stale in-flight refresh can capture one made after a
>   quick re-login; AC-25's Redis assertion is unobserved red under the limiter-first mutation.
>
> - **2.2 `intake-saved-base-cvs`** (PR #14, `4c18557`, **verified, merged, released**) — the first data
>   the product keeps **on purpose**: a registered user saves up to five base CVs
>   (`MAX_SAVED_BASE_CVS_PER_USER`), labels, reuses and deletes them, and can delete the account.
>   A row's owner is a **sum type**, `GuestOwner | UserOwner` in `domain/identity/ownership.py`,
>   with no methods — authorization is `cv.owner == UserOwner(requester)`, an equality — stored as
>   two nullable FKs plus `ck_intake_base_cv_exactly_one_owner`, translated in the mapping alone
>   (**ADR-0022**). **Reuse is a working copy** (`BaseCv.copy_from`: new id, new file, the source's
>   extraction, no re-extract), so no ownership graph crosses owners and the purge can never unlink
>   a saved CV's bytes; `copied_from_base_cv_id` is provenance with **no FK** (2.4's claim will read
>   it). The copy route is the first **transfer route** (ADR-0008 (f), Architecture; **retired in
>   2.4**). Deleting a CV
>   and erasing an account both go **rows committed, then files** (ADR-0006 amendment); erasure
>   holds `FOR UPDATE` on the user row so a racing upload cannot be cascaded away with its file
>   uncollected — AC-32 proves it with two real connections, the loser blocked and then refused
>   `UserNotFound`. Erasure lives in `retention` (`EraseAccount`, `AccountDataPort`, a report that
>   *returns* unlink failures); identity's `DeleteOwnAccount` verifies the password before any read;
>   the operator path is `erase-account` (Commands). The saved list is a **read model**
>   (`SavedBaseCvSummary`, `char_length(extracted_text)` in SQL — the column is never selected).
>   React: `/account` gains the list and account deletion, the workspace a picker above the
>   dropzone, and sign-out now crosses tabs by `BroadcastChannel`. One expand-only migration
>   (`1a2676aa3759`) whose **downgrade refuses** while any user-owned row exists.
>   **2094 backend and 749 frontend tests** (2085 at implement; nine added at `/verify`). Measured (T30): list at the cap p95 **6.8 ms** (budget
>   100); delete p95 **9.0 ms** (150); delete-account p95 **66 ms** (400); on the production image, a
>   purge of 100 sessions beside 500 saved CVs **0.59 s** (10 s), then the sweep reclaiming **0 of
>   500** saved files. **Copying a 10 MB CV first measured p95 1.57 s against 1.0 s — and the
>   cause was not the disk** (10 MB write+fsync+read ≈ 20 ms): the fixture held 10 M characters, and
>   `ExtractedText.__post_init__` re-counted them in a Python generator (~0.4 s) on **every load, on
>   the event loop**, three loads per copy because `select()` runs the `TypeDecorator` even on an
>   identity-map hit. Nothing capped extracted text (a 171 KB DOCX expanded to 39.5 M chars). Fixed
>   (T30b, owner decision): the count in C (`len - count(" ")`, exact over all 1,114,112 code
>   points; 406 → 3.7 ms), `get` via the identity map with an id-only existence check (a bare
>   `session.get` hit broke S-33) and the repository pinning what it hands out (the map is weakly
>   referenced), and **`TEXT_TOO_LONG` at `MAX_EXTRACTED_CHARACTERS=250000`**, enforced per page /
>   paragraph while extracting. Re-measured: a realistic 10 MB PDF copy p95 **47 ms**; the worst the
>   cap allows (250 k chars) **21 ms**; a guest list of 5 × 250 k **28 ms**. **A slow file operation
>   is not evidence about the disk until the disk is measured alone.** **AC-58 holds**: `git diff main --stat` over `domain/tailoring`,
>   `application/tailoring` and `infrastructure/llm` is empty. T31: the release script needs no
>   change; production's `identity_retired_refresh_token` holds **0** rows (OQ-7 re-armed: 100 k or
>   2.3).
>   **2.2's `/verify` took two rounds, and both MAJORs were tests that could pass without their
>   claim — no production code changed.** (1) AC-49/50's privacy test was still in its RED-era
>   "soft" shape: every step ran only `if _ok(previous)` and the prompt assertion sat under
>   `if fake_llm_request is not None`, so a copy that started answering 409 would have skipped the
>   whole flow **green**. A test written soft so it can go red before GREEN must be **hardened in
>   the commit after GREEN** — every step a hard status assertion, the fake's call count asserted
>   before its arguments. (2) S-4/S-41/S-43/S-45, AC-29's data column and AC-30 had no assertion;
>   S-45's log lines are the operator's only signal that an erasure left orphans. Also found:
>   AC-27's "committed before unlink" read the row on the request's **own** session, where an
>   uncommitted `DELETE` is already invisible — it now reads from `engine.connect()` and goes red
>   without the commit. Spec rows S-5, S-12 (owner: keep `identity.user_missing`), S-23 and S-44
>   were amended to what the code does on purpose. The manual pass found nothing; one observation
>   for 2.3: typing into `/register` while the boot refresh is in flight can be wiped by a remount.
>
> - **2.3 `tailoring-application-history`** (PR #15, `0b01537`, **verified, merged, released 2026-09-30**)
>   — a signed-in user's tailoring becomes **account data** (**ADR-0023**): the workspace follows the
>   credential, so signed in, the posting, the run and its exports are **born user-owned** and the
>   run references the saved CV directly. Every row has its final owner from its first `INSERT`, no
>   ownership graph crosses owners, and **no transfer route is added** (the AST scan's set is still
>   `{POST /api/base-cvs/copies}`, which now has no first-party caller until 2.4 decides its fate).
>   2.2's owner shape (`GuestOwner | UserOwner`, two nullable FKs, `ck_<table>_exactly_one_owner`)
>   is extended to `posting_job_posting`, `tailoring_run` and `export_job`; a job's owner is always
>   its run's. Twelve **`/api/me/`** routes (postings, runs, history, re-open, edit, delete, inline
>   download, exports, poll, file) answer to the bearer alone and **share one handler body with
>   their guest twin** (`routers/_{posting,tailoring,export}_handlers.py`, taking a resolved
>   `requester: Owner`, the rate-limit principal and a path prefix), so the two cannot drift.
>   `Cache-Control: no-store` on every one; `expires_at: null` means "kept until you delete it".
>   User caps: 500 runs, 500 postings, 20 exports **per run** (settings, in-code defaults).
>   History is a **read model behind a query port** (**ADR-0024**): `TailoringHistoryQuery` in the
>   domain, one Core statement under `infrastructure/persistence/queries/`, two LEFT JOINs with the
>   **owner in the join condition** (a deleted or foreign CV is never lent a label),
>   `left(text, 140)` in SQL, no document column selected, and **keyset pages on
>   `(requested_at, id)`, never `OFFSET`**. The cursor is an unsigned domain value
>   (`HistoryCursor`), base64url at the boundary. **Deleting a saved CV leaves history intact**:
>   `tailoring_run.base_cv_id` becomes a dangling reference with no FK, and "CV deleted" is **derived
>   at read time**, never stored. A run whose CV vanished between request and worker fails with the
>   tenth reason, **`base_cv_deleted`**, from `running`, **before the paid call**, not retryable
>   (ADR-0014 amendment). **Deleting a history entry lives in `retention`** (`EraseHistoryEntry`,
>   `HistoryEntryDataPort`): 409 while `queued`/`running`, then three `DELETE … RETURNING`s, so
>   **the rows that went and the files to unlink are one set**. Rows are committed, then files.
>   Unlink failures are returned, and the orphan sweep reclaims the survivor. Account erasure now
>   takes runs, postings and exports and their derived export keys, and its `FOR UPDATE` on the
>   user row also serializes run and export inserts (AC-37, two real connections). React: the
>   client's own port, **`features/scope/`**: `WorkspaceScope = guest | account(userId)` and a pure
>   `scopeMap()` giving paths, credential and query-key root together. **One hook set serves two
>   workspaces**: T28 threaded it through every hook with **749/749 guest tests unedited**. Account
>   keys live under `['auth','account',userId]`, so 2.1's sign-out drops them. `/` is a gate
>   (booting / unavailable + Retry / guest workspace / account workspace), plus `/history` (Load
>   more, confirm dialog, no optimistic removal) and `/history/:runId/:document`, and no "24 hours"
>   copy in account scope. One expand-only migration (`03494836ce30`) whose **downgrade refuses**
>   while a user-owned row or a `base_cv_deleted` run exists. **2689 backend and 839 frontend
>   tests.** Measured (T36): history first page p95 **6.3 ms** and page 20 **6.0 ms** over 500
>   runs (budget 100; Index Scan on `ix_tailoring_run_user_id_requested_at`); re-open **11.4 ms**
>   (100); delete an entry with 20 files **13.4 ms** (250); delete-account with 500 runs and 1 005
>   files **280 ms** (2 000; the disk alone 13.6 ms); launch **51.7 ms** (300); queued → running
>   **83.4 ms** (2 s). On the production image, a purge beside 4 100 user files takes **1.52 s**
>   (10 s) and deletes 0 user rows; the sweep takes **1.16 s** and reclaims **0 of 4 100**. The one
>   observation: at page 20 the posting join reads all 500 of the user's postings. That is cheap at
>   the cap, so look again if the cap rises. T38 (production, read-only): the three tables hold
>   **0** rows, so the migration runs as written. `identity_retired_refresh_token` also holds **0**
>   (OQ-11 re-armed: 100 k rows or 2.5), and the uploads volume is empty (OQ-15's baseline).
>   **AC-54 holds**: `git diff main --stat -- api/src/tailorcraft/infrastructure/llm` is empty and
>   `LlmPort` is byte-identical. Unlike 2.1/2.2, `domain/tailoring` *does* change (the owner, the
>   tenth reason, `history.py`), which is why the proof narrowed to the adapter and the port.
>   **`/verify` took three rounds** (2689 → **2691 backend, 840 frontend** tests, green twice).
>   AC-51 was proven in a real browser: a 25 s delayed boot refresh, text typed into `/register` and
>   `/login` mid-flight, and it survived. AC-59: one real-Gemini account run, `llm_duration_ms`
>   **6268 ms**, end-to-end **6.62 s** (budget 15 s). Round 1's review found a race that the plan had
>   called impossible. A history deletion's `DELETE … WHERE NOT EXISTS (run)` could not see a new
>   run's **uncommitted** `INSERT` on the same posting (there is no FK on
>   `tailoring_run.job_posting_id`), so either order left a run pointing at a deleted posting. It is
>   closed by two locks (below). Round 2 found the first fix disabled by default: a
>   `refuse_missing_posting=False` constructor flag that existed because 78 test seeds inserted runs
>   over postings that did not exist. The seeds were fixed first (`3c7e80d`), then the flag was
>   removed (`3b57daf`), and the check always runs. **A safety check whose default is off protects
>   only the callers that remember it.** Also fixed: a history delete now removes the run's cached
>   detail and exports and re-reads postings; the history row's link comes from `runLink`; AC-33's
>   concurrent-delete test now really stages the overlap. Carried to the PR: the cleanup `gather`
>   in `test_me_history_delete.py` has no timeout, and eleven seed comments still say the refusal
>   is "through the request route's composition root".
>
> - **2.4 `workspace-registration-cta`** (PR #16, `2427b67`, **verified, merged, released 2026-10-01**) —
>   a guest who tailored and then registers or signs in **keeps the work** (**ADR-0025**), by an
>   explicit offer that names it (*Keep them in my account* / *Not now*) and never automatically: a
>   guest cookie identifies a browser, not a person. `POST /api/me/guest-work/claim`, no body: the
>   bearer is a dependency, `tc_guest` is read **in the handler body**, nothing is ever minted
>   (ADR-0008 (g)). **One transaction**: the session row `FOR UPDATE`; four `UPDATE`s, each setting
>   both owner columns in one statement (`intake_base_cv` minus working copies, `posting_job_posting`,
>   `tailoring_run`, `export_job`); the working copies `DELETE … RETURNING file_key`; the session's
>   `DELETE`; one commit; then the dropped copies' files. No row is copied, no id changes, no file
>   moves (keys derive from ids), so a claimed run is in history at its original `requested_at` and
>   `/runs/{id}` becomes `/history/{id}`. 200 with five counts, **200 with zeros** when there is
>   nothing to claim (the session row is the idempotency key, and the claim consumes it); the cookie
>   is cleared iff one was presented; 10/h per user (`GUEST_WORK_CLAIM_RATE_LIMIT_PER_HOUR`, in-code
>   default, fails open). Caps bound creation, not transfer. In-flight work moves as it is, because
>   **the claim never bumps `version`**. That is mutation-proven (T23): with `version + 1` added, a run
>   claimed during its LLM call loses its paid result (`TailoringRunConcurrentlyModified`). **The
>   purge changed for the first time since 1.6** (ADR-0018 (a)): `delete_session -> bool`, and a
>   session whose `DELETE` removed nothing is counted `sessions_skipped` with **none of its files
>   unlinked**, since they may be a user's now. A guest write landing after a claim is 401
>   `guest_session_expired` (four `add`s translate the guest FK). Every race in ADR-0025's lock table
>   is staged on real connections, with the overlap proven from `pg_stat_activity`. **The copy route
>   is retired** (it now answers 405, because the path still matches `/api/base-cvs/{id}`); its
>   readers (`copied_from_base_cv_id`, `origin`, the badge) stay until the contraction trigger in the
>   roadmap (T37 read **0** such rows in production). React: `features/claim/`, a registration CTA on
>   a succeeded guest run, and the offer on `/` and on the guest run page (Keep →
>   `/history/:id/:document`, same id). No migration, container, queue or volume.
>   **2816 backend and 929 frontend tests.** Measured (T35): a claim at every guest cap (5 CVs, 10
>   postings, 20 runs, 40 exports) p95 **11.1 ms** (budget 300); an empty one p95 **9.3 ms** (50); on
>   the production image, a purge of 100 sessions beside 100 claimed users' 4 100 files **1.59 s**
>   (10 s), deleting 0 user rows or files, and the sweep reclaiming **0 of 4 100**. **AC-49 holds**:
>   `git diff main --stat` over `infrastructure/llm` and `execute_tailoring_run.py` is empty.
>   **T36 found the release order wrong** (R-7; Infrastructure footguns).
>   **`/verify` passed in one round** (0 CRITICAL, 0 MAJOR; `make check` green three times). The
>   owner amended four spec rows to the shipped behaviour: AC-24's `no-store` covers the responses
>   the **handler** builds (the dependency's 401 and the app's 503 carry no account data; a
>   `/api/me/*` middleware is the fix if ever wanted); AC-33 is **405** with an `Allow` header
>   without `POST`; AC-18's outcome is `TailoringRunConcurrentlyModified`; AC-34's wire value is
>   `llm_timed_out`. The manual pass on `:8080` with real Gemini walked CTA → register → offer →
>   Keep → `/history/{id}/cv` (the edit kept, run `version` unchanged, the PDF re-downloaded
>   byte-identical by sha256 and inode) → *Not now* → second claim → the other tab's history link →
>   account deletion (every row and file gone); 1 797 log lines, zero PII markers, both
>   `identity.guest_work_claimed` lines captured and neither carrying a session id. **Carried:** the
>   purge CLI stopped on `sessions_deleted == 0`, so a batch that only skipped ended a run early
>   (**fixed in 2.5**, T6 `8714389`: it stops on `deleted + skipped == 0`); OQ-12 (`__Host-tc_guest`) is
>   recorded, not fixed; a guest refetch after claiming a run still *in flight* is untested (none on
>   a succeeded run). **From 2.5, a value object's skeleton gets a no-op `__post_init__`**, so a
>   "refused" test goes red on `DID NOT RAISE` rather than on the skeleton's `NotImplementedError`
>   (T5's red was the latter, accepted on 2.3's precedent).
>
> - **2.5 `identity-email-verification`** (PR #17, `eeec430`, **verified, merged, released
>   2026-10-03**) — the first **outbound channel**: account mail.
>   Registration stops enumerating and a forgotten password can be reset. Three ADRs: **ADR-0026**
>   (account mail is a port, `AccountMailPort`, sent by the worker over SMTP submission on the
>   standard library, STARTTLS required, not a guarded egress), **ADR-0027** (a pending
>   registration, confirmed by a one-time link) and **ADR-0028** (a reset proves the address and
>   revokes every login). Amended: ADR-0005 (four queues), 0008 (h) (register 202 for every
>   address; 409 moves to confirmation), 0018 (b) (a second timer job, on by default), 0019 (a)
>   (`jobs.identity_token_sweep`), 0020 (sweep + reset revocation), 0021 (the `Origin` set is
>   **eight**; the new limiters fail closed). **An unconfirmed sign-up is not an unverified
>   `User`**: it is a `PendingRegistration` in its own table (one per address, newest wins by
>   `ON CONFLICT (email) DO UPDATE`, ≤ 24 h), so "every `User` proved its address" is true by
>   construction and "what may an unverified account do?" has the answer *nothing*. **Register has
>   no branch**: Origin → per-IP → parse → per-address (3/h) → policy → one argon2 hash → one upsert
>   → enqueue the **id** → 202, empty, no cookie — the same statements for every address, with zero
>   `UserRepository` calls (structural test, mutation-proven). The **worker** looks the address up
>   and sends either *Confirm your email* or *You already have an account* (no token). Reset-request
>   is the same shape with no hash at all. **The worker mints the token** (256 bits, 43 chars,
>   SHA-256 at rest, in the URL **fragment**, masked `OneTimeToken(***)`), so no credential rides
>   the broker, and **commits the hash, then sends**: a crash between the two leaves no mail rather
>   than a dead link, and the issued-once guard (`token_hash IS NULL`) makes a redelivery `SKIPPED`.
>   Confirmation needs an explicit click (mail scanners fetch links) and **does not sign in** (the
>   pre-hijack). Reset-confirm replaces the hash and **deletes every `Login` and reset** in one
>   transaction, signing nobody in; React signs the tab out as `password_changed` and broadcasts.
>   **The login/reset race**: `LogIn` re-checks `SELECT 1 … WHERE password_hash = :seen FOR SHARE`
>   after its verify, so a reset committed mid-verify is `InvalidCredentials` and a login first
>   makes the reset wait and then delete it; `DeleteOwnAccount` re-checks under **`FOR UPDATE`**
>   (`get_for_update`; see the lock-upgrade footgun). Lock order: the **user before any reset**,
>   everywhere. **The identity token sweep** (`retention.sweep_expired_identity_tokens`, hourly,
>   unconditional, batches of 1 000, inclusive expiry) deletes expired pending rows, resets and
>   logins — **on by default**, unlike the purge, because every code path already refuses those
>   rows; `/health/ready` reports `jobs.identity_token_sweep.overdue` (rows expired > two intervals
>   ago) as a fact. **A fourth queue, `mail`**: four kombu members, the release check lists four.
>   **Mailpit** in dev (UI `127.0.0.1:8025`) and CI; the dev override pins `MAIL_SMTP_*` in
>   `environment:` so a real `.env` cannot mail strangers. React: `features/accountMail/`
>   (`/confirm-email`, `/reset-password`, `/reset-password/confirm`, *Check your email* with *Send
>   it again*), the fragment read once and stripped, `Referrer-Policy: no-referrer` on the SPA.
>   Migration **`b1b518fe84b1`** (expand-only, two tables plus `ix_identity_login_expires_at`; the
>   downgrade refuses nothing — the rows are one-time and ≤ 24 h). `check-settings` has **eleven**
>   refusals. **3612 backend and 1021 frontend tests.** Measured: T42 on the production image with
>   production argon2, n = 200 interleaved: register new vs. existing Δ **0.56 %**, reset-request
>   known vs. unknown Δ **1.63 %**, login wrong-password vs. unknown-email Δ **0.42 %** (≤ 10 %);
>   T41's burst after the `ignore_result` fix: 2 × 3 600 resets, 0 probe timeouts, `/health/live`
>   p50 **3.9 ms**. AC-56 p95s (n=50, api direct, not via `:8080` — the dev limiters forbid n=50 there): register **60.1**, confirm **6.1**, login **59.1**, reset-request **7.8**, reset-confirm **58.9 ms**; 202 → Mailpit **122.5 ms**. AC-57: a 10 000 × 3 sweep **0.167 s** (2 s); the `overdue` probe p95 **5.86 ms** (20). **AC-62 holds**:
>   `git diff main --stat` over `infrastructure/llm`, `domain/tailoring` and
>   `application/tailoring` is empty (`infrastructure/tailoring/queue.py` changed — the enqueue
>   fix, outside the LLM boundary). Spec rows amended during `/implement`: AC-5 (`MailNotDelivered`
>   carries `smtp_code`), V-23 (`SENT` has no reply code), AC-14 (`FOR UPDATE`, not `FOR SHARE`),
>   AC-32 (eight `Origin` routes — the spec's "seven" missed `delete-account`), AC-41 (the
>   mutation is `<=` → `<`; the spec had it backwards).
>
> - **3.1 `tracking-application-board`** (branch `feature/tracking-application-board`, **verified
>   (reviewer PASS round 2, 2026-10-06; manual pass T33)**, PR pending, nothing merged or released)
>   — a signed-in
>   user's job search on a board. A **seventh bounded context, `tracking`** (**ADR-0029**), chosen
>   by whose fact it is: a run's status is a fact about a paid call, a card's stage is a fact about
>   the user's life. One `TrackedApplication` aggregate per **succeeded, user-owned run**, referenced
>   through tracking's own **`TrackedRunRef`**: `domain/tracking` imports **no sibling context**
>   (owner's T0 amendment; an AST allow-list pins it to the standard library, `domain/shared` and
>   `domain/identity`), "only a succeeded run" is the use case's rule, and the one seam is
>   `TrackedRunRef(run.id.value)` in `application/tracking/`. (ADR-0029 said `NewType`; the ids are
>   frozen dataclasses like every other id — a dated correction is in the ADR.) The owner is a
>   **`UserId`, not ADR-0022's `Owner`**: `user_id NOT NULL → identity_user ON DELETE CASCADE` and
>   **no guest column**, so the purge and the claim cannot reach a card by schema (a test asserts
>   the column is absent). A card holds a stage, an optional `ApplicationTitle` (trimmed, 1–120,
>   no `Cc`), `tracked_at`, `stage_changed_at` and `version` — no notes, salary or dates. **Six
>   stages, any → any** (a permissive machine, unlike `TailoringRun`'s strict one; the aggregate
>   says why at the point of contradiction); the invariants are time and **optimistic concurrency by
>   `version`**, checked before the no-op rule, plus the mapper's `version_id_col`. The board is a
>   **read model behind an unpaginated query port** bounded by the 500-card cap (**ADR-0024
>   amendment (a)**): one Core statement, three LEFT JOINs with the owner in every join condition,
>   `left(text, 140)`, no document column. **Deleting a history entry takes its card** by its own
>   `DELETE … RETURNING` in `retention` (ADR-0023, ADR-0006 (f) amended); **account erasure** takes
>   cards by the FK cascade. **No FK on the run**: the race is closed by 2.3's pattern — `add`
>   INSERTs, **then** takes the run `FOR KEY SHARE` and refuses if it is gone; the history delete's
>   card `DELETE` is a separate statement with a fresh snapshot (both orders staged on real
>   connections). Migration **`7e43a47327ec`** (expand-only, one table; the **downgrade refuses**
>   while any row exists). Settings, in-code defaults: `MAX_TRACKED_APPLICATIONS_PER_USER=500`,
>   `TRACKING_WRITE_RATE_LIMIT_PER_HOUR=600` (user scope, fails open); no new `check-settings`
>   refusal. Five bearer-only routes under `/api/me/` (`GET /board`, `POST /tracked-applications`,
>   `PUT …/{id}/stage`, `PUT …/{id}/title`, `DELETE …/{id}`), `no-store`, no transfer route. React:
>   `features/tracking/`, **`/board`** (six labelled columns), an *Add to board* button on succeeded
>   account runs and history rows, and a **Move to** native `<select>` with **native HTML5 drag as an
>   enhancement** sending the identical request. **The codebase's first optimistic update**: a move
>   snapshots, writes the stage, rolls back **only its own card** on refusal, moves focus to the
>   card's control in its new column, and invalidates the board only when the **last** move in
>   flight settles. Retitle, untrack and track are not optimistic. **4095 backend and 1137 frontend
>   tests** after `/verify` (4094/1124 at implement), green twice in a row. Measured (T30): board at the 500 cap p95 **48.0 ms** (budget 150), body **330 KiB**
>   (400 KB); track **17.8 ms**, move **8.4**, retitle **7.6**, untrack **7.5**; deleting a history
>   entry with a card and 20 files **22.3 ms** (250); main bundle **+4.96 kB gzip** (15). T31: no
>   container, queue, volume, beat entry or `.env` change; `deploy.yml` stops worker and beat at
>   line 126, before the migration at line 140. **AC-41 holds**: `git diff main --stat` over
>   `infrastructure/llm`, `domain/tailoring` and `application/tailoring` is empty; the board makes
>   no LLM call and queues no task. Found on the way: the `FOR KEY SHARE` lock-mode bug (below),
>   fixed for tracking in `e93f4fd` and **still present in 2.3's posting lock, left for the owner**.
>   **`/verify` took two rounds.** Round 1 NEEDS CHANGES (0 CRITICAL, 1 MAJOR, 9 MINOR). The MAJOR:
>   **overlapping moves were untested** — the mutations "restore the whole snapshot on a refusal" and
>   "refetch on every settle" both left the suite green; `471ad69` stages two cards, A refused
>   (409/503) while B is held, and records M1/M2/M3 red. MINORs fixed: `no-store` on handler-raised
>   refusals (`1721ea2` RED → `d2cce6d`); the cap copy from the server's message, not a hard-coded
>   500; a board-retention note on `/account`; focus never stolen and returned to the Move control on
>   refusal; untrack/retitle skip the board refetch while a move is in flight
>   (`invalidateBoardUnlessMoving`); *Edit title* disabled during the card's own move;
>   `data-drop-target` (`ae139b2` RED → `ead86c4`, a stale-row correction in its own commit →
>   `93b8f79`). Round 2 **PASS** (0 CRITICAL, 0 MAJOR). Every RED carries a recorded assertion
>   failure, no GREEN edited a test, and the two RED corrections (`a437aca`, `ead86c4`) are each
>   their own commit.
>   **T33's manual pass (2026-10-06, `:8080`, Chromium via Playwright):** register + confirm through
>   Mailpit; two **real-Gemini** runs (the second ~5.1 s end to end); *Add to board* from the run page
>   and from history (badge *On your board · To apply*); `/board`'s six regions; a drag To apply →
>   Applied sends `{"stage":"applied","version":1}`; a keyboard *Move to* (focus follows into the new
>   column; live region *Moved … to Offer.*); a second tab's move, then the stale tab's → 409
>   `tracked_application_version_conflict` with `no-store`, the alert *This application changed in
>   another tab — your board is up to date.*, refetched; retitle and clear; **with the worker
>   stopped** a move is 200 in 51 ms (T-6); a deleted saved CV → *CV deleted* on the cards; a
>   tracked history entry's deletion (the dialog names "its card on your board") → card gone; a
>   removed card → its history entry stays, *Add to board* back; `erase-account --dry-run` prints
>   `…; tracking: 500 tracked application(s)`; account deletion → 204 and every row gone (user,
>   cards, runs, postings, CVs, logins = 0). 16 997 log lines (api/worker/beat/nginx) grepped for the
>   email, the marker tag, posting text, card titles, CV filename and CV body: **0 hits**; positive
>   controls: the user id 100 hits, 90 tracking event lines. **Firefox drag not exercised** (only
>   Chromium was available to the harness).
>   **Found in T33, by no test:** (1) **AC-44 failed, then was fixed** (`54fae03`): at 500 cards on
>   the production bundle an optimistic move committed in p50 162 / p95 179 ms (budget 50), still
>   ~135 ms with the PUT held 2 s — pure render cost: **1000 `BoardCard` renders per move** and 500
>   more on settle. Two causes: inline per-card props on an unmemoized `BoardCard`, and mainly
>   **TanStack's structural sharing matching array items by index**, so a re-sort handed every
>   shifted card back as a new object (Conventions, React). Fix: `memo(BoardCard)`, stable id-taking
>   callbacks, cached drag props, constant removal states, `structuralSharing: shareCardsById`.
>   After: 2 renders per move, 1 on settle; optimistic move p50 **23 ms**, p95 **51.9 ms**, max 53.3
>   (n=20) — p50 well inside, **p95 ~2 ms over the 50 ms budget, recorded as measured**. First paint
>   at 500 cards: commit p50 135 ms; commit + forced style/layout p50 **245 ms**, p95 **270 ms**
>   (budget 300). The dev build measured 640–730 ms, which says nothing about the budget. (2) The
>   Board link made the signed-in header **16 px too wide at 360 px** (scrollWidth 361 > clientWidth
>   345 beside the scrollbar); the nav now wraps (`c1ac844`). (3) A drag that moved the **wrong card**
>   3/3 was Playwright's `dragTo`, not the app (Conventions). (4) A 500 on account deletion was the
>   **seed script's** `file_key = 't33/<uuid>'`, which `FileRef` refuses on load; repaired by id, the
>   deletion answered 204 (Conventions).
>
> **1.6's `/verify` took three rounds and found four gaps a green suite of 1423 was happy with — and all
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
> invisible to a recording fake** — *the thing named is not always the thing acted on*: (1) a
> partial that survived while the live file beside it died (below); (2) a symlink whose **target**
> the orphan sweep destroyed — the scanner never follows links, `_resolve_contained` called
> `.resolve()`, which does, and the sweep is the first caller that deletes by a name it *discovered on
> disk*; (3) `_put_sync` opening `<key>.part` with a plain `open()`, writing through a link that
> `os.replace` then installed at the real key. **The uniform fix is stated once in the file store's
> module docstring**: `O_NOFOLLOW` through one `_nofollow_opener` on `put`/`get`, work on the
> descriptor (`fchmod`, not `chmod`); `unlink` for deletes; `_resolve_contained` resolves the parent
> and never the basename — a check-then-use, so the outer lock and never the only one.
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
> **What 1.5's `/verify` found on top of that, in code 1293 green tests were happy with** (the full
> stories are in `FORboehpyk.md`):
> - **The refused-link helper deleted the author's own characters** (`range [100k](150k)` → `range
>   100k`): one predicate asked two questions. `_is_refused_url` is the second one ("was this ever a
>   URL?"), and a one-character scheme is a drive letter (`len(scheme) > 1`). **Put a heuristic's
>   error on the side that shows too much** — under-stripping shows inert text at zero security cost.
> - **An error state whose only affordance reproduced the error** — the click's meaning now comes
>   from the view, not a second derivation from the row.
> - **Five failure-contract rows reached nobody** (`requestExport.isError` read nowhere); three
>   commit no row, so silence invited the very retry the 429 exists to stop.
> - **A test that could no longer fail** — an emitter's promise is "for any stream", so its fixture
>   needs a stream the pipeline would never produce (a raw `h4`).
>
> **Carried out of 1.5, each with an owner and a trigger:**
> - **Startup refusals never exiting under `uvicorn --workers N`** — **closed in 2.1** (T46): the
>   production `CMD` pre-flights `check-settings`; see the footgun below.
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
> **The first release ran on 2026-09-22 (PR #10, `b3acdc1`, deploy run 35788932871) and
> `cv.samolit.com` is live.** Every migration ran from empty to head, the pre-deploy dump and the
> three-queue check passed under `set -euo pipefail`, and the smoke check read `ready: true` with one
> worker. The purge is deployed but **off** in production (`scheduled: false`) until it has been
> rehearsed on the box. **PR #9's deploy failed at the config sync, harmlessly**: it was branched
> before the deploy wiring, so it ran the *old* workflow against the old target path and never
> reached the release step. That is the "a run uses the workflow file from its own commit" rule
> below, met in practice. The VDS serves
> **`cv.samolit.com`** through the Traefik already running there (the
> external `traefik` network, entrypoint `websecure`, resolver `le`). The stack lives in
> **`/home/boehpyk/www/tailor-cv`** and deploys as **`boehpyk`** (bash, `docker` group), with a
> dedicated key used for nothing else. The root `.env` was made by hand on the box: mode 600,
> generated hex passwords, `APP_ENV=production`, **`GUEST_PURGE_ENABLED=false`** until the purge has
> been rehearsed *there*. Checked over SSH rather than taken on trust.
> The four deploy secrets — `SSH_HOST`, `SSH_USER`, `SSH_DEPLOY_KEY`, `GHCR_TOKEN` (a classic PAT,
> `read:packages` only) — are **`production` environment secrets, not repository secrets**, so
> GitHub releases them only to a job that has already passed the required reviewer. The environment
> carries **two protection rules**, `required_reviewers` (the owner) and `branch_policy` (protected
> branches only), verified by reading the API. **From here on, every merge to `main` is a real
> release waiting for an approval** — and a run uses the workflow file *from its own commit*, so an
> older branch merged later deploys with the older script.
> An earlier revision of this paragraph said the environment had zero protection rules; that was
> true when written and **was repeated for a whole slice after it stopped being true.**
>
> **The release script gained two steps the docs had promised and it never ran:** a `pg_dump` into
> `backups/` (700/600, newest ten) *before* anything changes, with the datastores brought up first so
> it works on an empty box; and a check that the worker consumes `celery`, `tailoring` **and**
> `export`. That check is anchored on `* {'name': '<queue>'` because **the exchange is also named
> `celery`** — an unanchored match reports the default queue present when it is gone, proved by
> deleting its line from real `inspect active_queues` output. nginx also carries
> `traefik.docker.network=traefik` itself, since the provider flag belongs to another stack's Traefik.

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
dependency (the destination, authorized first) and reads `tc_guest` **in the handler body** (the
source): never through `require_guest_session`, whose 401 would break the claim's idempotency, and
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
  **Two TanStack timing traps from 2.1.** `gcTime: 0` garbage-collects an *unobserved*
  `setQueryData` entry on the next macrotask, so an absence/presence assertion on one tests GC, not
  your code — AC-44's logout-scoping test was measuring the collector, not logout. Its client now
  uses `gcTime: Infinity` and asserts both halves (`['auth','me']` gone, the guest query kept). And `isPending` updates on the next notify, so a
  same-tick double click reads `false` twice: guard a submit with
  `queryClient.isMutating({ mutationKey }) > 0`, which `mutate()` updates synchronously.
  **TanStack's default structural sharing matches array items by index (3.1, `54fae03`).** Data
  that re-sorts (a moved card) gets every shifted item back as a new object, so `memo` on the item
  component re-renders them all: 1000 `BoardCard` renders per move at 500 cards. Keep identity by
  id with a `structuralSharing` function (`shareCardsById`), and give the memoized item stable,
  id-taking callbacks and no inline object props.
  **A credential is not server state:** the access token lives in one module store read through
  `useSyncExternalStore` — never `useState`, the query cache, or browser storage (AC-35 greps for it).
  **Navigate-then-sign-out races `RequireAuth` (2.2).** A data router commits navigation in a
  transition, and `RouterProvider` from `react-router` **ignores `navigate(…, { flushSync: true })`**
  (only `react-router/dom`'s honours it), so the synchronous sign-out re-rendered the still-mounted
  guard and account deletion landed on `/login`. Fixed in the guard: anonymous with reason
  `account_deleted` goes to `/` with the notice. And TanStack skips a mutate-level `onSuccess` on
  an unmounted observer — so work that must survive the unmount runs **before** the sign-out.
  **Whose workspace this is comes from the route's scope, never from "is someone signed in?"
  (2.3).** Hooks take paths, credential and key root from `useScopeMap()` (`features/scope/`).
  The default, with no provider, is the guest map, and a guest key is byte-for-byte what it was.
  **A hand-built path bypasses the scope.** `DocumentTabs` and the editor's leave-guard hard-coded
  `/runs/${id}/…`, so on a history run the Cover letter tab jumped to the guest route and a tab
  switch counted as leaving. Build every link from `runLink(scope)` (fixed T32, mutation-pinned
  T35).
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
  **Pick the statistic for the workload, too.** 2.1's AC-20 p50 stayed green across five runs with
  argon2 inlined on the loop: login is mostly I/O, so the blocked fraction stays under ½ and a
  median cannot see it. The owner amended AC-20 to the loop's **unavailable fraction**. Its first
  formula, `Σt / wall`, is ≈ `t̄ / (pace + t̄)` — it measured *latency*, not blocking, and climbs on
  any slower box. The shipped one is `Σ max(0, t − baseline) / Σ t` with a same-run no-load
  baseline: invariant to a uniform slow-down (not to the pace, which is a module constant). Healthy
  0.775–0.799, mutated 0.971–0.974, bound **0.88** — a thin margin; **watch its first CI runs**.
- **Inject a fault *below* the floor you are testing.** Monkeypatching an adapter's public method
  replaces the adapter — including its own `except Exception` floor, which is the thing an
  "unexpected failure → 503" row exists to prove. Patch the library call the adapter wraps, so the
  floor still stands between the fault and the route (I-45's correction, `1ef5afb`).
- **A skeleton that changes a signature breaks existing tests** — the mechanical rename it forces
  (2.2: `guest_session_id=` → `owner=GuestOwner(…)` across 15 files) lands in the **skeleton's**
  commit, by `qa`, with no assertion changed in meaning — never in a RED or GREEN commit.
- **A pinned field that needs data an AC forbids loading is a design contradiction, not a test
  bug** — take it to the owner. 2.2's `character_count` vs AC-52 became a read model.
- **Tests that tested the harness (2.2, three more):** `gcTime: 0` collecting an unobserved seed
  (again); two concurrent requests through the shared-`AsyncSession` `app` fixture give
  `IllegalStateChangeError` → 503 — races use `concurrent_app`, a real session per request; a
  helper returning `BaseCv.upload(…)` without `release_events()` leaked a pending event into the
  publisher — a stand-in for a loaded aggregate must hold none. A sweep that walks its whole root
  needs a per-test `tmp_path`, not the shared `upload_dir`, or aged files leak between tests.
- **When the implementer finds a RED test wrong, the correction is its own commit** (`8880439`,
  `15e1f5b`), verified against the spec, so the GREEN commit still edits no test.
- **Commit order is part of the plan, and every commit must be green (2.3).** Two orders in 2.3's
  plan could not be green. First, 2.2's tripwire ("`guest_session_id` is `NOT NULL`") **fires by
  design** in the commit that lands the migration, yet the plan retired it later. It was retired in
  the migration's own commit (`7da2c4a`), with AC-2's test generalized to "nullable **and**
  `ck_<table>_exactly_one_owner`" on all four tables, and the survives-a-purge proof it asked for
  (`57f7087`) landed right after the adapters with nothing in between. Second, **a mapping must not
  land before its column**: a mapped column the table lacks breaks every load, so the migration
  (T13) was committed before its mapping (T12). When a task list's order cannot be green, reorder
  on purpose and say so in the commit body.
- **The pre-commit hook tests the working tree, not the staged commit.** Committing several commits
  from one dirty tree gives each commit a gate that checked more than the commit contains. Stash
  the rest per commit (`git stash push --keep-index --include-untracked`), so each hook sees exactly
  its commit. A `set -e` script must pop the stash even when the commit fails.
  **`TDD_RED=1` refuses a commit with no staged test file**, so a frontend GREEN cannot commit
  while a backend RED on the same branch is still red (2.3's `/verify`: `react-dev`'s fix waited
  for `api-dev`'s GREEN). Order a round so each RED's GREEN lands before the next RED, or commit
  the hardening before the RED in the same file. `--no-verify` is still never the answer.
- **A test seed that only flushes dies with a refused request** (2.3, `fa40793`). The `app` fixture
  joins with `create_savepoint` and rolls back when a request errors, so an uncommitted seed goes
  too. 25 tests then read 0 rows where the seed should have been. Seeds on the test's session
  **commit**: that releases a savepoint, and the outer rollback still isolates the test. Guest
  tests never met this because their inputs arrive through routes that commit.
- **A race test must stage the race at the moment the spec names, not before the request** (H-33).
  A delete that commits before the edit is sent is a clean 404. The spec's 409 with
  `current_version: null` needs the delete to commit **between the edit's read and its write**.
  That is injected by wrapping the repository's `save` on `concurrent_app` to commit a `DELETE` on
  another connection first. The test also asserts that the delete really landed mid-request.
- **A domain module may not import a sibling context, even where the layers allow it.**
  `retention`'s allowlist test caught `HistoryEntryInProgress(status: TailoringRunStatus)`. The
  status now travels as a plain `str`, and `delete_history_entry` takes a bare `UUID`. Retention
  reaches other contexts' rows through its own ports, never their types.
- **A skeleton's scaffolding is dead code once the real methods land.** Remove it in its own
  commit (2.3: `RequestTailoringRun.users`, the superseded `_for_session` pair). Moving the tests
  that call the old methods comes first and is mechanical.
- **Every domain `Protocol` is bound by a composition root or exempted by name**
  (`tests/integration/test_port_bindings.py`, 2.3's T23b). The Protocols are discovered, not
  listed. The plan had assumed this test already existed, and it did not. **Check that a gate
  exists before the plan leans on it.**
- **A rule over "every table that has X" is a test that discovers the tables from the catalogue**
  (2.4). The claim must re-key every table with a `guest_session_id`; a forgotten fifth table would
  be cascade-deleted with the session, silently. The test reads `information_schema.columns`, so a
  new guest-owned table the claim misses fails by name instead of losing data.
- **Widening a port's return from `None` silently breaks every monkeypatched stub of it** (2.4).
  `delete_session` began returning `bool`; four 1.6 stubs still delegated and dropped the answer, so a
  really-deleted session read as `sessions_skipped` and the CLI's loop stopped early. mypy cannot see
  through `monkeypatch`. When a return type gains meaning, grep for every stub and fake of that method
  and correct them in their own commit, before the GREEN that reads the value.
- **A fake needs the isolation of the thing it fakes** (2.4, `ed66fbe`). A fake server that scoped
  two `/api/me/` lists by bearer and answered the third to anyone made "B never sees A's claimed
  data" fail against a correct client. Check the fake before the code.
- **A Core `UPDATE` does not touch the identity map** (2.4, `d37481f`). Through the shared-session
  `app` fixture, a test still holding a seeded aggregate read its pre-claim `GuestOwner` after the
  claim re-keyed the row with Core, because `select()` on a map hit keeps loaded attributes: a 404
  against a correct handler. `session.expire_all()` after the write, with a comment naming the
  harness. Production has a session per request.
- **Re-seed before you remove.** Tests that use the thing being retired only as a seed
  (`BaseCv.copy_from`) are re-seeded first, in their own commit (`b197d18`), so the removal commit
  deletes only that thing's own tests.
- **A cast that is unnecessary before a type narrows and necessary after** gets a one-line,
  commented `eslint-disable` in the commit before the narrowing, removed in the commit after it
  (`5a546fb`, `a763ffb`). Each commit stays honest and green.
- **A RED that cannot be reproduced is recorded as such, never forced.** 2.2's "`/register` typing
  wiped during the boot refresh" had no `booting` branch to remount in the current code. AC-51 was
  committed as a regression guard **with no recorded red** (`2c01310`), and the real-browser
  attempt moved to `/verify`.
- **A test that pins the Alembic head as a revision id breaks on the next migration** (2.5,
  `0cc192c`) — red on the revision alone while every schema fact still holds. "At head" means
  `ScriptDirectory.from_config(…).get_current_head()`; pin a revision id only where the test is
  about *that* revision.
- **A race test's staging seam must survive the fix** (2.5, `672c5a5`). The lock-upgrade test first
  hooked `confirm_credential_unchanged` — the very method the fix stopped calling — so its "was the
  race staged?" guard failed against the correct code. Stage at the *moment* the spec names (after
  the verify, before the first lock), wrapping every entry point that can occupy it.
- **A spy installed before the test's setup records the setup** (2.5, `feec266`). A
  `replaceState` spy saw the harness's own `openLink()` put the token in the URL. Clear the spy
  after setup, and give the absence assertion a positive control (the app made ≥ 2 calls).
- **pytest in the api container cannot see repo-root files** (2.5). Compose and nginx config checks
  live in `scripts/git-hooks/` as Python checks run by pre-commit on the **staged** content, plus a
  make target (`compose.mail.check` `252baa8`, `nginx.referrer.check` `6471056`). CI does not run
  hook checks — a known gap, chosen by the owner.
- **A RED test that disagrees with the spec is corrected, not satisfied** (3.1, `a437aca`). Two
  T21 tests sent `marker + "\n"` as a title and expected 422, but AC-2 trims *first* and then
  refuses `Cc`, so that title trims to a valid one. The implementer reported it at T23 instead of
  bending `ApplicationTitle` to pass; the correction (an **interior** LF, `marker + "\nx"`) landed
  in its own commit, assertions unchanged, still red, before GREEN — so GREEN edited no test.
- **A strict xfail is the holding pen for a production defect `qa` finds** (3.1, `d380d4b` →
  `e93f4fd`). `qa` never edits production code, so the lock-mode test went in as
  `xfail(strict=True)` with the finding in the commit body; the fix's commit removed only the
  marker. Strict matters: a fix that lands without removing it turns the suite red (XPASS).
- **`make test file=<one integration file>` can fail at collection** with `ExportJob has no
  attribute '_id'`: the mappings are loaded by a fixture the lone file never reaches. Select with
  `k=` instead. And **`make web.test file=` ignores `file=`** — it runs the whole Vitest suite.
- **An `EXPLAIN` over a table holding only one user's rows correctly seq-scans** (3.1, AC-31).
  The planner is right that a scan is cheaper than the index when every row matches. To assert
  index use, seed other users' rows first, so the predicate is selective.
- **A performance budget on a list is measured on the production bundle in a real browser** (3.1,
  T33). AC-44 passed every unit render test and failed in Chromium at 500 cards (p50 162 ms vs.
  50); the dev build's 640–730 ms first paint said nothing either way. Serve the production
  bundle on `:8080` through Playwright route interception (origin and cookies unchanged).
  **`requestAnimationFrame` is throttled to ~1 Hz on an occluded desktop window even while
  `document.visibilityState` reads `visible`**, so a rAF-based paint timer read ~1.8 s; time a
  commit with a `MutationObserver`, then force style/layout (`offsetHeight`) and time that too.
- **Verify a harness's gesture before blaming the app** (3.1, T33). Playwright's `dragTo`
  presses the mouse on the source, **then scrolls the target into view**; Chromium starts an HTML5
  drag from wherever the pointer is *then*. With a tall target column (~191 px of scroll) the drag
  picked up the card below the intended one, 3/3. Stepwise `mouse.move` drags always moved the
  right card.
- **Seed data goes through the domain's grammars** (3.1, T33). A seed wrote
  `intake_base_cv.file_key = 't33/<uuid>'`; `FileRef` refuses that on load, so account erasure
  500'd on a row real data can never produce. Build seeded values with the value objects (or copy
  their grammar), or the erasure path tests the seed.
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
  exchange (`task_default_exchange`, unchanged through slice 2.5's fourth queue), the healthy state is
  **four members in the single set `_kombu.binding.celery`** (`celery`, `tailoring`, `export`,
  `mail` since 2.5) — `_kombu.binding.tailoring` and friends do not exist at all, and finding "one
  member each in four sets" describes a broker with four exchanges, not this one. A stale binding is
  a **fifth** member of `_kombu.binding.celery`, which is exactly the shape the `SREM` example above
  deletes. Also:
  `CELERY_BROKER_URL` is db `/1` — `redis-cli` without `-n 1` reads db `0`, where every set is empty,
  which reads as a clean broker for the wrong reason.
- **Every container running application code appears in the deploy's `pull` list and in the
  image-verification loop** — here `api`, `worker`, `beat`. In the previous project the worker was in
  neither and ran a stale image for four releases; the only symptom was behaviour not matching the
  source. The worker and beat are also **stopped before the new API starts and through the
  migration window**, not merely restarted after it. Until 2.4 the script started the new `api`
  first and stopped them second, so for a few seconds an old worker and beat ran beside a new API.
  For 2.4 that meant a 2.3 purge (which unlinks without asking whether its `DELETE` removed the row)
  could run beside a claim. T36 found it by reading `deploy.yml` against the plan's claim, and
  `a9c9ea9` moved the stop first. Every doc had said "stopped for the migration window", which was
  true and hid the order. **Confirm a release-order claim by reading the script, every slice that
  relies on it.** A run uses the workflow from its own commit, so the fix takes effect with 2.4's
  own release.
- **A single-file bind mount is pinned to the inode at container create, and `scp`/`rsync` replace
  the file under a new inode.** The deploy synced `docker/nginx/default.conf` and ran
  `docker compose up -d api nginx`; the nginx service spec had not changed, so nginx was not
  recreated and kept serving the first release's file. Measured 2026-10-03: host inode 258354,
  container inode 274226, `Referrer-Policy` count 0 in the container — **no nginx change reached
  production between 2026-09-22 and 2.5's release**, and nothing errored. The release now runs
  `up -d --force-recreate --no-deps nginx` and then compares the running container's config to the
  shipped file and greps `nginx -T` for `Referrer-Policy`, under `set -euo pipefail`. Confirm what
  the running process serves, not what was shipped. (Rejected: mounting a directory — `dev.conf`
  shares `docker/nginx/`, and it would also need a reload.)

- **`env_file:` outranks the image's `ENV`, so the root `.env` decides `APP_ENV` on a real box.**
  `.env.example` therefore defaults to `APP_ENV=production` (the safe value) and
  `docker-compose.dev.yml` pins `APP_ENV: dev` in `environment:` (which outranks `env_file:`), so
  dev-ness follows the override file you load rather than a value someone remembered to change.
  **The same holds for `PUBLIC_BASE_URL`**, the origin 2.1's `Origin` check trusts: the root `.env`
  carried a production-shaped value, so every auth `POST` from `:8080` was 403
  `origin_not_allowed`. `docker-compose.dev.yml` pins `PUBLIC_BASE_URL: http://localhost:8080`. Since 2.5 the **worker and beat** pin it too: the worker builds every emailed link from it, and `check-mail-compose.py` requires the pin (and empty `MAIL_SMTP_USERNAME`/`PASSWORD`) on all three.
  Recreating `api` to pick that up then died on `ModuleNotFoundError: jwt`: `make deps` syncs a
  *running* container, and a recreate goes back to the image, which predated the dependency. After
  a dependency lands, it is `up -d --build api worker beat`.
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
- **A startup refusal under `uvicorn --workers N` used to not exit the container — fixed in slice 2.1
  (T46, OQ-2).** The production `CMD` in `docker/api/Dockerfile` is now
  `sh -c "python -m tailorcraft.cli check-settings && exec uvicorn … --workers 2"`.
  `check-settings` (`infrastructure/check_settings_command.py`) builds `Settings` and runs the Celery
  stale-window check **once, in a single pre-flight process**, before uvicorn is `exec`'d — so a
  refusal exits the shell non-zero and uvicorn never starts, and success replaces the shell with
  uvicorn (still PID 1, still receives signals directly). It covers **all eleven** refusals the API
  process has (five since 2.1, six added in 2.5), the same code paths a request would hit, not a
  copy:
  1. `GEMINI_API_KEY` empty under `APP_ENV=production` (`Settings` validator).
  2. `JWT_SIGNING_KEY` missing, whitespace, the `.env.example` placeholder, or shorter than 32 bytes,
     under `APP_ENV=production` (`Settings` validator, AC-22/I-46).
  3. `TEST_REDIS_URL` resolving to the same `(host, port, db)` as a live Redis role — broker, result
     backend or cache (`Settings` validator).
  4. `TAILORING_STALE_AFTER_SECONDS` at or below the Celery hard time limit
     (`tasks/limits.refuse_stale_windows_within_time_limit`).
  5. `EXPORT_STALE_AFTER_SECONDS` at or below the same hard time limit (same function).
  6–10. (slice 2.5, `APP_ENV=production` only, `Settings` validator) `MAIL_SMTP_HOST` empty;
     `MAIL_SMTP_SECURITY=none`; `MAIL_SMTP_USERNAME`/`MAIL_SMTP_PASSWORD` empty; `MAIL_FROM_ADDRESS`
     not an `EmailAddress`; `PUBLIC_BASE_URL` not `https://`. **The box's `.env` must carry the
     `MAIL_*` values before 2.5's image starts there**, or the API never starts.
  11. `MAIL_TOTAL_DEADLINE_SECONDS` at or above Celery's soft limit (120), every environment
     (`tasks/limits.refuse_mail_deadline_within_soft_limit`).
  It prints `settings ok` and exits 0, or `check-settings: <sentence>` on stderr and exits 1 — never
  a secret: a `MisconfiguredSettings` sentence names the variable, not the value; a pydantic
  `ValidationError` is reduced to field names and error types.
  **Measured (T46, the same method as 1.3's T36):** production image, `APP_ENV=production`, a dummy
  `GEMINI_API_KEY` (so refusal 2 is the one that fires) and the `.env.example` placeholder
  `JWT_SIGNING_KEY` → the container exits **1 in 0.78 s**, stderr carries the `check-settings:`
  sentence and never uvicorn's banner. With a valid 32-byte-plus key and the other defaults → stdout
  prints `settings ok`, uvicorn starts both `--workers 2` processes, and `/health/live` returns 200.
  **The in-process guards described below still exist and still matter** — `check-settings` only
  changes what happens in the container built from this Dockerfile with this `CMD`. Anyone who runs
  `uvicorn` directly (a hand-rolled `docker run` overriding `CMD`, a different image, a future
  entry point that forgets the `&&`) is back to the old behaviour: the guard fires at import in every
  worker process and the supervisor respawns the crashing import forever, never exiting.
  When a release's readiness check never goes green, read the logs for a settings refusal before
  suspecting the network — `check-settings` makes that refusal loud, but only if it ran.
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
  **Consequence met in 2.1: cutting the chain also cut asyncpg's `constraint_name`**, the only object
  that knew *which* constraint refused a write. The listener now copies the allow-listed identifiers
  onto the error it keeps; a repository recognises a violation with
  `database.violated_constraint(exc) == "uq_…"` and **never by parsing the message**, which is
  withheld on purpose and is prose for a person, not a format for a program.
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
  **That entry flush also walks straight past a version check** (2.1, `save_rotation`). The
  repository writes a Core `UPDATE … WHERE version = :v`; with the rotated `Login` still attached,
  `begin_nested()` first flushed the ORM's own `UPDATE` with **no** version predicate, so a race
  loser overwrote the winner's token and both answered 200. `expunge` the aggregate before the
  SAVEPOINT; on success re-attach it clean with `set_committed_value`.
- **`NOT EXISTS` in a `DELETE` cannot see another transaction's uncommitted `INSERT`, and waiting
  for a lock does not refresh it** (2.3's `/verify`). A history delete of the form `DELETE FROM
  posting … WHERE NOT EXISTS (SELECT … FROM tailoring_run …)` deleted a posting that a concurrent
  request had just used for a new run. Under READ COMMITTED, a `DELETE` that waits on a row that
  was only *locked*, not updated, proceeds on its **original** snapshot and does not re-check the
  subquery. The fix is two statements and two locks. The deleting side runs `SELECT … FOR UPDATE`
  on the posting, then the `DELETE` as a **separate** statement, which gets a fresh snapshot. The
  inserting side (`SqlAlchemyTailoringRunRepository.add`) takes `FOR KEY SHARE` on the posting
  **after** its `INSERT`: the INSERT's FK check takes the owner row first, so account erasure
  (user row `FOR UPDATE`, then cascades) meets it there. Taking the posting lock first makes a
  lock cycle. The refusal is **unconditional**. The first fix hid it behind an off-by-default
  constructor flag because test seeds inserted runs over postings that never existed. Fix the
  seeds, never the default.
- **`get_settings()` under `APP_ENV=test` still returns the *dev* `database_url`.** Only
  `tests/conftest.py` swaps in `test_database_url`, by overriding Alembic's option and the engine
  fixture. A probe or cleanup script that builds its own engine from `settings.database_url` — even
  with `APP_ENV=test` — is pointed at the dev database, and one such script's `DELETE FROM
  tailoring_run; DELETE FROM identity_guest_session` emptied dev through the cascades during 1.4's
  verify. Anything that deletes must name `test_database_url` explicitly and assert the URL contains
  `_test` before the first statement.
- **The local `tailorcraft_test` runs out of columns — PostgreSQL never gives back a dropped
  column's slot.** `DROP COLUMN` only marks the attribute dropped; it still counts toward the
  1600-column limit until the table is rewritten, and `VACUUM FULL` does not renumber it. The
  migration up/down/up tests drop and re-add `tailoring_run`'s columns on every run, and the test
  database persists between runs, so each suite run burns ~10 slots. On 2026-09-23 the table held
  **16 live columns and 1580 dropped ones**, `upgrade head` died on `TooManyColumnsError`, the fixture
  left the schema three revisions short, and **428 unrelated tests errored**, blocking a docs-only
  commit. CI never sees it: its database is new every time. Signature: the migration tests fail
  first, then everything touching `export_job` reports `UndefinedTableError`. **Fixed (PR #12):**
  `_migrated` drops and recreates `public` at the start of every session, behind a guard refusing
  any database not named `*_test`, so a local run now starts where CI does. The count of dead
  columns stays flat at one run's worth instead of growing.
- **Alembic runs a revision in one transaction, so `NOT VALID` → `VALIDATE` staging inside one
  revision releases no lock.** The `ACCESS EXCLUSIVE` taken by `ADD CONSTRAINT … NOT VALID`, and
  the write lock of a plain `CREATE INDEX`, are held **until the revision commits**. The later
  `VALIDATE` running under `SHARE UPDATE EXCLUSIVE` buys nothing. 2.3's `03494836ce30` is staged
  that way, and its docstring's "blocks neither reads nor writes" is true only per statement. It is
  harmless at production's 0 rows (T38). If one of those tables ever grows, split the revision
  (constraint `NOT VALID`, commit, then `VALIDATE`; `CONCURRENTLY` indexes in a non-transactional
  revision) and set a `lock_timeout`, so a busy table fails the deploy fast rather than queueing
  every writer behind it.
- **A `FOR SHARE` re-check followed by `FOR UPDATE` on the same row in one transaction is a lock
  upgrade, and two concurrent callers deadlock on it** (2.5, T14 → `643f701`). `DeleteOwnAccount`
  re-checked the credential `FOR SHARE`, then erasure took the user row `FOR UPDATE`: two correct
  deletions both held SHARE and both waited for UPDATE — one 503 (`deadlock_detected`), and a loser
  arriving after the winner's commit read 403 instead of 401. **Take the strongest lock you will
  need first.** The re-check is now `get_for_update` plus a hash comparison; `LogIn`, which never
  upgrades, keeps `FOR SHARE`.
- **`AsyncResult.__del__` unsubscribes synchronously on whichever thread drops it** (2.5,
  `74e793e`). With a Redis result backend, `send_task` subscribes the process's one shared PubSub
  connection to the task's result channel (from the `to_thread` worker) and returns an
  `AsyncResult` the adapter discards; its `__del__` then unsubscribes **on the event loop**, two
  threads drive one non-thread-safe connection, and the loop blocks on the socket — a burst of
  reset requests wedged the production API for > 60 s, `/health/live` included. **Every
  `send_task` from the API passes `ignore_result=True`**: `send_task` reads only its own kwarg and
  never the app's `task_ignore_result`, so the config key would not have fixed it. Visible only
  under concurrent enqueue bursts on the production image (faulthandler found the frame). Nothing
  reads a task result any more, so the result backend is unused — dropping it is the owner's call.
- **`.with_for_update(key_share=True)` alone renders `FOR NO KEY UPDATE` on PostgreSQL**, a
  *stronger* lock; `FOR KEY SHARE` needs `read=True` as well (`.with_for_update(read=True,
  key_share=True)`). 2.5's `/verify` noticed it; 3.1 fell into it again in the tracking repository
  and a **statement-capture** test found it (T19, held as a strict xfail; fixed in `e93f4fd`). Under
  the stronger lock the deletion race still held, but a concurrent edit of the run would have waited
  on a track. **The same defect remains in 2.3's
  `infrastructure/persistence/repositories/tailoring/tailoring_run.py:186`** (the posting lock) —
  still correct, only stronger than intended, and **left for the owner**. Compile the query and read
  the SQL; a keyword argument's name is not documentation.
- **`base64.urlsafe_b64decode` silently discards characters outside its alphabet**, so
  `"not-base64!!"` decodes. Anything decoded from a client (the history cursor) uses
  `b64decode(s, altchars=b"-_", validate=True)`. Parse integers from it as digits only, because
  `int()` accepts a sign, spaces and underscores. Every failure is one domain error, raised
  `from None` and never echoed.
- **nginx must not run `ngx_http_realip_module`.** One layer reconstructs the client IP, not two.
  nginx forwards the headers; the application decides. Two trust layers that each look right in
  isolation is the trap, and the symptom is a rate limiter keyed on the proxy's address — one global
  bucket instead of one per visitor. **`TRUSTED_PROXY_HOPS` is the same trap wearing a number
  instead of a module name**: it must equal the count of proxies *we* run in front of the api, which
  on the box is **two** — Traefik, then nginx — not one. It read `1` from Phase 1 through 2.1's
  `/verify`, which would have keyed every visitor, on every IP-scoped rate limiter, on Traefik's own
  address; with 2.1's login/register limiters failing closed that is a site-wide lockout, not a
  slow-degrade. The box reads `2` as of 2026-09-26 (read over SSH, T31). The dev override pins it
  to `1`, since dev has only nginx in front.
- **On FastAPI 0.141, a dependency's teardown runs *after* the response is sent.** `get_session`'s
  commit therefore cannot change the answer: a failed commit ships a 200, or a 401 claiming a
  deletion that never landed. Any response that must reflect a committed write **commits inside the
  handler** — the reuse/expiry deletion behind a 401 (caught and *returned* as a response, so the
  dependency never takes its rollback branch), logout's cookie clear (only after the commit), even
  `/me`. Measured, not read in a changelog.
- **FastAPI 0.141 no longer flattens `app.routes` on `include_router`.** The real `APIRoute`s sit
  behind `_IncludedRouter.original_router.routes`, so a walker over `app.routes` finds nothing and
  passes vacuously. Descend, duck-typed (the class is private).
- **A removed route answers 405, not 404, when its path still matches another route.**
  `POST /api/base-cvs/copies` matches `/api/base-cvs/{base_cv_id}` (GET, PATCH, DELETE), so Starlette
  refuses the method before any handler runs. Assert what the removal protects (405, and an `Allow`
  header without the method), not a 404 that only extra code could produce.
- **Two access tokens minted in the same second for one user are byte-identical** — no `jti`, and
  `iat`/`exp` are whole seconds. Never key anything (a cache, a denylist, a test's "it changed") on
  token identity.
- **`JWT_SIGNING_KEY`'s `.env.example` placeholder is 33 bytes**, so a ≥ 32-byte check alone accepts
  it. The production validator refuses it **by name**, and never with a `Field(min_length=…)`, whose
  `ValidationError` renders `input_value` — the key.
- **The WHATWG URL parser strips tab, CR and LF**, so a `next=/\t/evil.example` navigates as
  `//evil.example` — an open redirect straight past a prefix check for `//` and `/\`.
  `safeNext` refuses any control character rather than listing the three.
- **`tailorcraft-api:local` is whatever the dev override last built** — the `development` target,
  `--reload` and all. A "production image" measurement must `docker build --target production` under
  its own tag, or it measures the wrong thing and supplies confidence anyway.
- **httpx's cookie jar will not send a `Secure` cookie over plain HTTP**, so a production-mode app
  measured without TLS never gets `tc_refresh` back and every refresh reads as "not signed in".
  Parse `Set-Cookie` and re-attach it by hand in such a script.

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
