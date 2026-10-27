# Slice history

The per-slice record that used to sit in CLAUDE.md's Status block, moved here verbatim on
2026-10-26 to keep CLAUDE.md small. Newest facts are at the top of each entry; the full stories are
in [FORboehpyk.md](./FORboehpyk.md).

> **Status: nineteen slices shipped (1.1–1.6, 2.1–2.5, 3.1–3.3, 4.1–4.4, and the `__Host-tc_guest`
> security slice); slice 4.4 `admin-guests-and-storage` was released 2026-10-15, and Phase 4's
> two-week gate clock runs to 2026-10-29; Phase 2 is closed, its gate met on 2026-10-03. Slice 3.1 `tracking-application-board` was
> merged as PR #21 (`bfb98a9`) and released 2026-10-06 (deploy run 37529074626). The
> `identity-host-prefixed-guest-cookie` slice was merged as PR #22 (`998c4b4`) and released
> 2026-10-09 (deploy run 37852977749). Slice 3.2 `export-pdf-layout-templates` was merged as PR #23
> (`a092053`) and released 2026-10-09 (deploy run 37855115482). Slice 3.3
> `workspace-rate-limit-retry-feedback` was merged as PR #25 (`279a0b7`) and released 2026-10-09
> (deploy run 37951163281).** **Slice 4.1 `identity-user-roles` (Phase 4's first) was verified
> 2026-10-10 (reviewer PASS round 1), merged as PR #28 (`26472b1`) and released the same day**
> (deploy run 38074030810; the owner was bootstrapped as the first admin by `grant-role`). **Slice
> 4.2 `admin-users` was verified on 2026-10-11 (reviewer PASS round 2), merged as PR #30
> (`16fbe47`) and released the same day** (deploy run 38161380305). **Slice 4.3 `admin-content`
> was verified 2026-10-12 (reviewer PASS round 1; MINORs 1/3/4 fixed, PASS round 2), merged as PR
> #32 (`5e9bce2`) and released the same day** (deploy run 38297123697). **Slice 4.4
> `admin-guests-and-storage` was verified 2026-10-15 (reviewer PASS round 1; MINOR 1 fixed red-first, `49e2aef` → `5cfd637`), merged as PR #34 (`97a213e`) and released the same day** (deploy run 38704657392; the production purge was rehearsed from `/admin/storage`, backlog 2 → 0). Slice 2.1 was verified (two rounds, 2026-09-25), merged as PR #13
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
>   (**fixed in 2.5**, T6 `8714389`: it stops on `deleted + skipped == 0`); OQ-12
>   (`__Host-tc_guest`) was recorded, not fixed (closed by the `__Host-tc_guest` slice,
>   2026-10-08); a guest refetch after claiming a run still *in flight* is untested (none on
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
> - **3.1 `tracking-application-board`** (PR #21, `bfb98a9`, **verified (reviewer PASS round 2,
>   2026-10-06; manual pass T33), merged, released 2026-10-06**, deploy run 37529074626)
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
> - **`identity-host-prefixed-guest-cookie`** (**implemented 2026-10-08, verified, merged as PR #22
>   `998c4b4`, released 2026-10-09**; it satisfies 3.2's AC-44) — the guest cookie is
>   **`__Host-tc_guest`**: `Secure` in every environment, `Path=/`, no `Domain` (**ADR-0010 (c)**,
>   discharging (b)'s session-fixation finding). A **hard cut**: `tc_guest` is never read again,
>   because a dual-read window keeps honouring a planted cookie and migrate-on-read would promote
>   one into the protected name. A guest mid-session at release loses that ≤ 24 h workspace. One
>   constant and one attribute dict in `guest_session.py`; `clear_guest_cookie(response)` lost its
>   `settings`. Dev on plain-HTTP `:8080` works because browsers treat `localhost` as a secure
>   context (Chromium 145, probed and walked end to end; Firefox not exercised). **Every test client
>   is `https://testserver`**: httpx stores a `Secure` cookie over `http://` and never sends it
>   back, so reverting that switch turns **186** tests red. The RED tests spell the literal
>   `"__Host-tc_guest"`, since a test that imports `COOKIE_NAME` cannot fail on a rename.
>   `secure=False` mutated → 7 red. No migration, setting or `.env` change. **Carried (roadmap
>   OQ-16): `tc_refresh` has the same exposure** — a sibling host can plant a refresh cookie that
>   signs the victim into the attacker's account; its `Path=/api/auth` cannot take `__Host-`, so it
>   is its own slice, on OQ-12's trigger.
>
> - **3.2 `export-pdf-layout-templates`** (PR #23, `a092053`, **verified 2026-10-08, released
>   2026-10-09**, deploy run 37855115482; AC-44 met — the `__Host-tc_guest` slice merged first, PR
>   #22) — a PDF export can be **Classic** (1.5's stylesheet, moved verbatim under a digest pin),
>   **Modern** or **Formal** (**ADR-0030**; ADR-0016 and ADR-0017 amended). `LayoutTemplate` is a
>   **closed `StrEnum`** in `domain/export`, recorded on `ExportJob`, **PDF only**: `request` refuses
>   a PDF without one (`LayoutTemplateRequired`) and a DOCX with one (`LayoutTemplateNotApplicable`,
>   422 `layout_template_not_applicable`), before `cls()`. The default is the use case's
>   (`DEFAULT_LAYOUT_TEMPLATE`), never a setting. The invariant is held **twice**: by the aggregate
>   and by **`ck_export_job_layout_template_only_for_pdf`**, a paired CHECK on the new nullable
>   `export_job.layout_template`. **"Is this the same request?" widened; "is this file out of date?"
>   did not**: `find_latest_for_key` keys on (run, document, format, **layout**, run version),
>   compared `IS NOT DISTINCT FROM` so DOCX is unchanged, while staleness stays `run_version` alone — a
>   layout switch stales nothing, an edit stales every layout. Caps count every layout. **The port
>   changed**: `DocumentRendererPort.render(…, layout_template)` and `find_latest_for_key(…,
>   layout_template)` take a required keyword; the port speaks `LayoutTemplate` and never CSS.
>   `infrastructure/export/layouts.py` holds three checked-in stylesheet constants and
>   `stylesheet_for` (no `@import`, `url()`, `@font-face` or interpolation — an AST test; system fonts
>   only — Formal embeds Liberation Serif); `render_pdf` takes the stylesheet as a keyword, and
>   `layout_template` joins the three render log lines. Migration **`b10d1c777b0a`** (down
>   `7e43a47327ec`; back-fills PDFs to `classic`; the **downgrade refuses** while a non-classic row
>   exists). React: a native radio-group `LayoutPicker` with committed WebP previews
>   (`make layout.previews`, a stylesheet-digest drift test), the choice lifted to `RunPage` and shared
>   by both tabs, **stored nowhere**: pre-selection is **derived** from the run's newest PDF job, else
>   Classic. Jobs are matched per layout; only a PDF body carries `layout_template`. **4385 backend
>   and 1196 frontend tests.** Measured (T27, production image): render p95 per layout × document
>   80–105 ms (budget 1 000 and ≤ 2 × Classic; worst ratio 1.07); a 20 000-character CV p95 759 / 771
>   / 807 ms (5 s); fixture PDFs 10–14 KB (200 KB); `POST` → `ready` p95 **178 ms** (10 s, dev
>   worker); main bundle **+0.88 kB gzip** (3). T28: production `export_job` held **3 rows, 2 PDF**,
>   so the back-fill and `ADD CONSTRAINT` take milliseconds; no package, container, queue, volume,
>   beat entry or setting. Found on the way: WeasyPrint's `::marker` crash, a CHECK that forced T11
>   and T12 into one commit (`37c7424`), two T8 cap tests that contradicted the step order
>   (`cc1bd0e`), five T24 tests that raced the loading state (`6cf4628`), Vite inlining the previews
>   (Conventions; Infrastructure footguns).
>
> - **3.3 `workspace-rate-limit-retry-feedback`** (PR #25, `279a0b7`, **verified and released 2026-10-09**, deploy run 37951163281;
>   reviewer PASS round 1; its six MINORs fixed in a follow-up PR, below) — a refusal says **when** it ends, and "still working" says **how** (**ADR-0031**;
>   ADR-0014 amended). *Reads retry, writes hold*: after a 429 every re-clickable control (tailor,
>   *Try again*, export per document × format, both uploads, posting — *Paste instead* stays usable
>   on a fetch 429 — claim, login, register, reset, *Send it again*) is disabled until `Retry-After`
>   passes, says so (*"at 16:01"* beyond 90 s in the **browser's locale**, *"in N seconds"* at or
>   under), and re-enables with a `role="status"` and no focus move; **nothing that writes retries by
>   itself** except autosave (version-guarded). Tracking, autosave and account deletion get the
>   wording only. The run and export pollers show *"trying again (attempt n of 4)"* and *offline*,
>   read from TanStack's own `failureCount` / `fetchStatus` — no new retry code, no jitter. After a
>   run fails `llm_rate_limited`, *Try again* holds **60 s**: `retry_not_before` on the three run
>   shapes, computed at the boundary (`completed_at + PROVIDER_BUSY_COOLDOWN`, a `match` closed by
>   `assert_never`), advisory, never stored — **no migration, no setting**. An AST test pins that
>   every router 429 (nine) carries `Retry-After` + `rate_limited`. `client.ts` reads every refused
>   body through one tolerant `errorFrom()`, so nginx's HTML 502 is an `ApiError`, not "check your
>   connection". React: `features/retry/` (`hold.ts`, `useHold`/`useErrorHold`, `HoldNote`,
>   `ConnectionNote`). **4454 backend and 1325 frontend tests.** Measured (T24): AC-24's `git diff
>   main --stat` over `infrastructure/llm`, `domain`, `application`, `rate_limit.py`, `settings.py`
>   and `alembic` is **empty**; run-detail `GET` p95 within **0.08 ms** of `main` (n = 200 × 3,
>   interleaved); main bundle **+2.18 kB gzip** — over the planned 2 kB, **AC-26 amended to 2.5 kB by
>   the owner** and recorded as measured. T25 (`:8080`, Chromium; Firefox not exercised): every
>   scenario passed. **Found, carried to `/verify`:** after a 429 hold ends, the alert still reads
>   *"…at 16:01"* beside *"You can try again now."*; `up -d api` alone leaves dev nginx on the old IP
>   (502 until `restart nginx`, pre-existing). **The follow-up (`fix/retry-feedback-followups`)
>   fixed /verify's six MINORs:** `useHold`'s `phrase` is `null` whenever nothing is held, and every
>   429 copy's `null` branch names no wait (*"Too many attempts."*, never *"a few minutes"*);
>   `SaveIndicator` words the pause as of receipt (`untilMs − retryAfterSeconds`); `HoldNote` mounts
>   its `role="status"` empty with the hold and fills it at release (`announce={false}` joins
>   `ExportControl`'s region), `ConnectionNote`'s is always mounted; AC-3's scan sees a literal
>   `429` and `rate_limited` only under `code`; the claim test spells its sentence. **A live region
>   mounted together with its text may never be announced** — mount it empty, then change it. Four RED corrections landed in their own commits
>   (`148f4e1`, `a8b626f`, `ae4c970`, `60fba47`), all **timing or spec-superseded copy** — see
>   Conventions.
>
> - **4.1 `identity-user-roles`** (PR #28, `26472b1`, **verified and released 2026-10-10**, deploy run 38074030810) — one role per
>   user, `user | admin`, and an `/api/admin` surface only an admin can see (**ADR-0032**; ADR-0008
>   amendment (i)). `Role` is a closed `StrEnum` on `User` (`register_with_password` sets `USER`, no
>   parameter; `change_role` records `UserRoleChanged`; `is_admin` is the **only** role check in the
>   codebase). Migration **`3d26e05edffe`**: `identity_user.role VARCHAR(16) NOT NULL DEFAULT 'user'`
>   (kept for good, for the deploy window) + `ck_identity_user_role_known`; the **downgrade refuses**
>   while an admin exists. **The role is never a claim**: `AuthorizeAdministrator` reads the row
>   (`users.get`, no lock) on every admin request, so a grant or revoke applies to the next request
>   and signs nobody out; the token still has five claims. `/api/admin` has a **router-level**
>   `require_admin` (bearer → 401, user gone → 401 `not_signed_in`, not admin → 404), pinned by a
>   structural walk with a positive control. **A non-admin's 404 is Starlette's**
>   (`starlette.exceptions.HTTPException(404)`, `{"detail":"Not Found"}`), byte-identical to an
>   unmatched path and compared against a live one — the **one** raise in the codebase that skips the
>   app envelope, on purpose. 4.1's only route: `GET /api/admin/access` → 204 `no-store`. Roles change
>   **only** by CLI (`grant-role` / `revoke-role`, `ChangeUserRole` under `FOR UPDATE`; Commands), no
>   HTTP writer, the last admin may be revoked. The foreign-database guard moved to
>   `infrastructure/persistence/database_guard.py`. React: `role` on `User`, an **Admin** link
>   between Board and Account iff `role === 'admin'` (a hint; the server's 204 is the authority),
>   and the codebase's **first code split**: `/admin` → `RequireAuth` → `AccountScope` →
>   `LazyBoundary` (a class; *Reload* on a failed chunk) → `Suspense` → `lazy(AdminPage)`; the
>   view is one union, **error first**, so a 404 beats a cached 204. The operator-access sentence
>   (`OPERATOR_ACCESS_NOTE`, Constitution §8) is on the guest workspace, the account promise and
>   `/account`. **4566 backend and 1360 frontend tests**, `make check` green twice. Measured: main
>   bundle **+1.24 kB gzip** (AC-35 **amended by the owner** 1.0 → 1.5 kB), admin chunk 0.81 kB;
>   `/api/admin/access` p95 **2.76–2.95 ms** on the production image (budget 30), `/me` within 1 ms
>   of `main`. AC-39 holds (`infrastructure/llm`, `domain/tailoring`, `application/tailoring`
>   unchanged). Five mutations recorded (Starlette → FastAPI 404: 1 red; router deps removed: 17;
>   `get_for_update` → `get`: 4; `is_admin` → `True`: 10; view order swapped: 1). **Found on the
>   way:** two harness defects in RED tests, each corrected in its own commit (`4c61a28`, `e58df70`;
>   Conventions); TanStack v5 refusing `undefined` data (the probe returns `true`); and **F-1, found
>   only by T26's manual pass**: the app's client turns `refetchOnWindowFocus` off, so a demoted
>   admin's open tab never asked again while a test on a bespoke client passed — fixed red-first
>   (`58b280a` → `bb6a4f1` → `21333e8`, the probe refetches `'always'` on focus). Firefox not
>   exercised. **`/verify` passed in one round** (0 CRITICAL, 0 MAJOR). T26's demotion was walked
>   again in Chromium: a focus 12 s after load, inside the 30 s `staleTime`, sent the probe again,
>   which answered 404, and the not-found view replaced the shell. The Admin link stays until the
>   next `/me` fetch, which R-26 allows. Three MINORs were tests that could not fail, hardened in
>   `1538047`, each proven by mutation: AC-13 had decoded one hand-minted token twice and now
>   decodes the tokens `/login` and `/refresh` issue to an admin; AC-23's scan now catches a Core
>   `.values(role=…)`; T3's refusal test, red only on `NotImplementedError`, was proven by moving
>   the guard. **Carried to 4.2:** the view puts every error ahead of a cached 204, so a transient
>   503 on focus replaces the admin page. Decide whether only a 404 outranks cached data.
>
> - **4.2 `admin-users`** (PR #30, `16fbe47`, **verified 2026-10-11, reviewer PASS round 2, released the
>   same day**, deploy run 38161380305; round 1's MAJOR, the search term reaching Sentry, fixed in `9984003`) — a user directory at `/admin` and three actions on an account: disable,
>   enable, erase (**ADR-0033**; ADR-0008 amendment (j), ADR-0024 amendment (b); notes on ADR-0020,
>   0028, 0032). **Disabled is a timestamp**: `User.disabled_at` (`None` is active), `disable(at,
>   logins_revoked)` / `enable(at)` record `UserDisabled` / `UserEnabled`, and a repeat records
>   nothing. Migration **`669b75917559`** adds `identity_user.disabled_at TIMESTAMPTZ NULL`; its
>   **downgrade refuses** while any account is disabled, because dropping the column would silently
>   re-enable them. **`DisableAccount` takes the user row `FOR UPDATE`, then deletes every `Login`**,
>   in one transaction (the user lock first, ADR-0028's order). After that, **two refusals**:
>   `LogIn` answers 403 `account_disabled` only after a **correct** password (a fresh read while
>   its `FOR SHARE` re-check holds the row; a wrong password stays the byte-identical 401), and
>   **every bearer request** reads the bit after the signature check (`UserRepository.is_disabled`,
>   one Core primary-key read, `False` for a missing row) and answers 403 `account_disabled`. The
>   second exists because an access token is stateless for 15 minutes: deleting logins alone would
>   leave the open tab working, paid runs included (OQ-1 = B). Enable restores the right to log in
>   and no session; a password reset still works and does **not** re-enable. Admin actions lock only
>   the target and never re-check the actor's role (ADR-0032's window, OQ-6). The **directory** is a
>   read model behind `UserDirectoryQuery`: one Core statement, no `password_hash`, newest first,
>   **keyset on `(created_at, id)`**, and search as **`strpos(email, :term) > 0`**, never `LIKE`, so
>   `%`, `_` and `\` are literal and there is nothing to escape (`EmailSearch`: stripped,
>   lower-cased, 1–254 characters, no `Cc`). The base64url cursor codec is **shared** with history
>   (`routers/_keyset_cursor.py`; `_history_cursor.py` is two wrappers). Five routes under
>   `/api/admin/users`, behind 4.1's router-level `require_admin`, `no-store`: `GET` (list, `q`,
>   `cursor`, `limit` 1–50 = 20), `GET /{id}` (the account and `GetAccountOverview`'s counts: CVs,
>   runs, postings, exports, cards, active logins), `POST /{id}/disable`, `POST /{id}/enable`,
>   `DELETE /{id}` (`EraseAccount`, unchanged, 200 with its report). A missing target is 404
>   `user_not_found` by a local translation, never the global map's 401. Self-disable and
>   self-erase are allowed; if the last admin disables itself, recovery is `grant-role` on another
>   account. React: `/admin` (list, search, *Load more*) and `/admin/users/:userId` (overview,
>   actions, confirm dialogs, not optimistic). The **descendant routes live in the lazy chunk** via
>   **`useRoutes`**, because `<Routes>` JSX pulled `createRoutesFromChildren` into the main chunk
>   (+0.69 kB gzip, over the 0.5 kB budget). A 403 `account_disabled` signs the tab out **without a
>   refresh** (reason `account_disabled`, broadcast) and lands on `/` with *Your account has been
>   disabled, so you were signed out.*; `/login` says *This account has been disabled.* 4.1's carry
>   is settled (OQ-5): **only a 404 outranks a cached 204**, so a transient 503 keeps the admin page.
>   **4777 backend and 1409 frontend tests** after the access-log fix (`make check` twice at
>   `5443e52`: 4773 / 1409). Measured: main bundle **+0.20 kB gzip** (budget 0.5), admin chunk
>   **4.04 kB** (8). On the production image, 10 000 seeded users, n = 200, p95: list page 1
>   **10.3 ms**, page 20 **10.6**, search `e` (every row) **11.7**, `z` **7.5**, overview of a
>   full user **8.7** (budgets 100); disable with 50 logins **9.8** (150); erase of a full user
>   (1 005 files) **415 ms** (2 000, n = 20). The bearer check costs `/me` **+0.51 ms p95** and
>   `/board` **+0.69** (budget +2). The list seq-scans (no index, as OQ-9 planned): ~3 ms at 10 000
>   rows. **Eight mutations recorded:** the bearer check off (4 red); `is_disabled` before the
>   re-check (1); the login `DELETE` before the user lock (4, and 2 races deadlock); `strpos` →
>   `LIKE` (2); the target's 404 through the global map (4); login's `AccountDisabled` branch
>   removed (3); the client refreshing on `account_disabled` (1); `AdminPage` error-first (1). AC-44
>   holds: `git diff main --stat` over `infrastructure/llm`, `domain/tailoring` and
>   `application/tailoring` is empty. **Owner amendments:** AC-24, a disabled login's refusal ends
>   its unit of work with an **empty commit**, as the route's other refusals do; AC-29, "existing
>   tests pass unedited" now allows **listed harness-only fixes**, and the one listed is `b1cc08f`
>   (Conventions). Two RED corrections landed in their own commits (`ab88358`, `618af74`;
>   Conventions). **T26's manual pass (Chromium 145, `:8080`, two contexts, Mailpit, 360 px)**
>   walked every step as AC-43 states, and **its log grep found what no test did**: the search term
>   rode `GET /api/admin/users?q=…` into the **nginx and uvicorn access logs** (2 hits). The owner
>   chose to strip query strings from access logs on **every** route: a `logging.Filter` on
>   `uvicorn.access` (`abef0a2`, mutation-proven) and a `noquery` `log_format` in both nginx configs
>   (`f77b1d6`); a marker re-check read 0 hits in either. **Not checked: Traefik's access log** (the
>   other stack on the box). **Firefox not exercised.**
>
> - **4.3 `admin-content`** (PR #32, `5e9bce2`, **verified and released 2026-10-12**, deploy run
>   38297123697; `/verify` PASS round 1, then MINORs 1/3/4 fixed red-first: `1e92e97`/`44c76ca` RED,
>   `6332bce`/`f6f47f4` GREEN, PASS round 2. `_Access` is a context manager writing one line per
>   request, `refused` on a `SQLAlchemyError` — **not** on any other exception, an accepted gap;
>   global 422/503 on `/api/admin` carry no `no-store`, also accepted) — an admin
>   lists, reads, downloads and deletes a user's saved CVs, postings, runs/history and exports
>   (**ADR-0034**; ADR-0008 amendment (k)). **No new use case**: the admin passes
>   `UserOwner(<path user_id>)` to the **unchanged** `/api/me/` use cases, so "may this admin see
>   it?" is `require_admin` and "is it this user's?" is the use case's own equality. There is no
>   `AdminOwner`. **AC-1 holds**: `git diff main --stat` over `domain/`, `application/`, `alembic/`
>   and the LLM paths is empty — no migration, setting, queue or container. Eleven routes under
>   `/api/admin/users/{user_id}/…` (`routers/admin_content.py`), a **router-level** `require_admin`,
>   `no-store` on every answer and refusal (`_admin_responses.refusal`, lifted from 4.2), and a
>   **local** 404 `user_not_found` for a missing target, never the global map's 401 (which would
>   sign the admin out). The shared `_tailoring_handlers` / `_export_handlers` bodies gained a
>   `translate=` keyword so the admin twin words a refusal its own way; history and saved-CV wire
>   mappers were lifted out of the `/api/me/` routers (behaviour-neutral, every test unedited).
>   `AdminSavedBaseCvResponse` is the saved shape **plus `extracted_text`**; the user's schemas are
>   pinned unchanged (AC-24). **One `admin.content_access` line per request** (`actor_id`,
>   `target_user_id`, `kind`, `action`, `item_id`, `outcome`): ids and closed words only. The two
>   deletes (`DeleteSavedBaseCv`, `EraseHistoryEntry`) answer **200 with their report**, where the
>   user's twins answer 204. React: three sections on `/admin/users/:id` (saved CVs, history with
>   *Load more*, the 20 most recent postings) plus CV, run and posting pages, all in the **lazy
>   admin chunk**; every body is a **text node** in a `pre-wrap` block, never
>   `dangerouslySetInnerHTML`, and a posting's source URL is text, never a link. **4892 backend
>   and 1484 frontend tests** at T17 (`make check` twice). Measured: main bundle **+0.06 kB gzip**
>   (budget 0.3), admin chunk **7.91 kB** (10). T16 on production images, one user at every cap,
>   n = 200 interleaved, admin p95: CV list **13.9 ms**, one CV (250 KB body) **12.1**, postings
>   list **44.0**, one posting **9.3**, history p1 **9.2** / p20 **9.3**, one run **10.9**, inline
>   md **11.3**, export list **12.3**, a 200 KB export **12.6**; deletes (n = 20) CV **15.2**, an
>   entry with 20 files **23.9** — every budget holds, admin-vs-user Δ at most **+2.3 ms** (limit
>   +5). **Mutations:** (2) `UserOwner(actor.id)` 48 red; (3) `UserNotFound` through the global map
>   12; (4) `no-store` off `refusal()` 27; (5) one access line removed 1; (6) `BodyText` via
>   `dangerouslySetInnerHTML` +4; (7) content keys outside `adminUserKey` 7; (8) a fresh,
>   non-committing `DeleteSavedBaseCv` 2. **(1), dropping the router-level `require_admin`, is an
>   equivalent mutant**: every handler also takes `actor: AdminDep`, so the graph walk stays green.
>   AC-30(a) now also pins `router.dependencies` (1 red). **4.2's `admin_users.py` has the same
>   shape** and the same blind spot. **Found by T14, a 4.2 defect:** the erase removed the
>   account's detail key with `exact: true`, so every content body under it (CV text, posting
>   text, documents) stayed cached for `gcTime`. Held as `it.fails`, fixed in `301c75a` (removal
>   by prefix). **T18's manual pass** (`:8080`, Chromium, two contexts, one real-Gemini run, markers
>   in every body and name field): every read and download agreed with the user's own; both deletes
>   agreed with the user's tab; 360 px held; a plain user saw the not-found view; 6 272 log lines,
>   **0 marker or email hits**. Limits: the 409 was staged on a **queued** run (worker stopped), not
>   a running one; a fetched posting's title carries no marker; **Firefox not exercised**. **Owner
>   amendments, 2026-10-12:** AC-26, each section has its own loading sentence (*Loading saved
>   CVs…*, …), because *Loading…* collided with the page's own; AC-29, the one listed harness edit
>   to an existing test is `ce358e4` (4.2's `UserPage` fake serves the three lists).
>
> - **4.4 `admin-guests-and-storage`** (**verified 2026-10-15, reviewer PASS round 1;
>   merged as PR #34 `97a213e` and released the same day**, deploy run 38704657392; T31's purge
>   rehearsal ran from the browser, `overdue` 2 → 0, `last_outcome: ok`; `GUEST_PURGE_ENABLED`
>   still `false` in production) — an admin sees **live guest sessions** and their data, erases one
>   before its 24 hours are up, and runs the purge and the orphan sweep **from `/admin/storage`**
>   instead of over SSH (**ADR-0035**; ADR-0008 amendment (l), ADR-0018 amendment (c); notes on
>   ADR-0006, 0019, 0024, 0034). **Fourteen routes** behind a router-level `require_admin`,
>   `no-store` on every answer and refusal: nine under `/api/admin/guest-sessions` (list, overview
>   with the guest's three lists, erase, and the guest twins of 4.3's six reads) in
>   `routers/admin_guests.py`; five in `routers/admin_retention.py` (`GET /api/admin/storage`,
>   `GET`/`POST /api/admin/guest-purge`, `GET`/`POST /api/admin/orphan-sweep` — a `GET` is the
>   preview and writes nothing anywhere: no lock, no heartbeat, no row, no file). **A guest is
>   reached by path, like a user in 4.3**: the admin passes `GuestOwner(<path session_id>)` to the
>   guest's **unchanged** use cases; the admin browser's own `__Host-tc_guest` is never read and no
>   session is ever minted (the transfer-route set is unchanged). A gone or expired session is a
>   **local** 404 `guest_session_not_found` on every guest route, never the global map's 401 (which
>   would trip the admin's refresh-on-401). **Erasing one session is the purge's own step**:
>   `PurgeExpiredGuestSessions`' per-candidate body was lifted into `purge_one(data, files,
>   candidate)` (T2, behaviour-neutral, every retention test unedited), and `EraseGuestSession`
>   calls it on one named session — rows committed, then files; a `DELETE` that removed nothing (a
>   claim got there first) is 404 with **nothing unlinked**; 200 `{files_unlinked,
>   unlink_failures}`. **An entry point owns its refusal codes**: a held `RedisPurgeLock` is the
>   CLI's exit 3, beat's quiet skip, and here **409 `purge_in_progress`**, nothing deleted, the
>   other holder's lock untouched; Redis unreachable still **fails open** (the run proceeds, 200
>   with `lock: "unavailable"`); a mid-batch raise is 503 `purge_failed` with no heartbeat. The sweep
>   still **fails closed** (cross-check down → 503 `orphan_check_unavailable`, not one file
>   unlinked) and takes no lock. Runs write the CLI's `retention.*` lines byte-identical (the purge's
>   failure loops lifted to `log_events.log_purge_failures`). **Storage is measured without reading a
>   file**: the sweep's existing walk (now with `ScannedFile.size_bytes` from its `lstat`) plus
>   **one** `owners_of` statement over the keys (`UNION ALL` over stored `file_key`s, chunks of
>   1 000), five buckets — guest, user, unreferenced, partial, unrecognized — counts and bytes only,
>   no name or key in the body. The **directory** is a read model behind
>   `GuestSessionDirectoryQuery`: one Core statement, never `token_hash` (statement-captured, AC-28),
>   live only, correlated counts, keyset on `(expires_at, id)` over the existing index. Two log
>   lines: guest reads write 4.3's `admin.content_access` with `target_guest_session_id` (`_Access`
>   lifted to `routers/_admin_access.py`); storage, purge, sweep and erase write
>   `admin.retention_action`. **No migration, setting, queue, container or beat change**; **AC-1
>   holds** (`git diff main --stat` over the listed domain/application/alembic/LLM paths: empty).
>   React, all in the lazy admin chunk: an admin nav (*Users* · *Guests* · *Storage*),
>   `/admin/guests` (*Load more*), `/admin/guests/:sessionId` (overview, three lists, *Delete guest
>   session*, not optimistic), 4.3's three read pages reused through a **subject** seam
>   (`AdminSubject = user | guest`, `subjectPath`, user keys byte-identical; no delete buttons for a
>   guest), and `/admin/storage` (storage table, purge and sweep panels with preview → confirm →
>   report, job health from `/health/ready`). **5100 backend and 1564 frontend tests** (`make
>   check` twice at T27, 5100 / 1561, plus T28's three). Measured: main bundle **+0.01 kB gzip**
>   (budget 0.3), admin chunk **12.33 kB** (14). T25 on production images, n = 200 interleaved, p95:
>   directory p1 over 10 001 live sessions **7.6 ms**, page 50 **7.9** (100); a capped session's
>   overview **28.9**, with heavy bodies **56.5** (150); guest reads vs. the guest's own routes at
>   most **+2.9 ms** (limit +5); erase of a capped session, 45 files **23.6** (300, n = 20); over
>   10 000 files storage **486.7** (1 500), sweep preview **427.5** (3 000); purge preview **141.5**
>   (300); a purge run of 100 capped sessions / 4 500 files **1 766 ms** (10 000); a sweep run
>   reclaiming 1 000 orphans **968 ms** (5 000). T26: `/health/live` turnaround p95 **1.82 ms**
>   during storage and a sweep preview over 10 000 files; walk inlined **379.87 ms**. **Fifteen
>   mutations recorded:** (1) `require_admin` off the router **and** `AdminDep` → any user 2 red;
>   (2) `GuestOwner` from a fixed id 15; (3) gone session through the global map 5; (4) unlink after
>   a skipped `DELETE` 1 at T9, 3 with T17's races; (5) unlink before `DELETE` 6; (6) held lock
>   ignored 1; (7) preview takes the lock and writes the heartbeat 1; (8) `OrphanScanAborted`
>   swallowed 1; (9) `owners_of` → "referenced ⇒ user" 1; (10) `token_hash` selected 1; (11) walk
>   inline (above); (12) `no-store` off `refusal()` 36; (13) one access line removed 2; (14) guest
>   key removed `exact: true` 0 at T22, 1 at T23; (15) guest `canDelete` 1 (CV page) at T22, the run
>   page's 1 at `055857f`, whose assertion had run before the posting query settled. **T28's
>   manual pass** (`:8080`, Chromium, two contexts, one real-Gemini run, a PDF, markers everywhere):
>   every guest read and download sha256-identical to the guest's own; erase → the guest tab's
>   *Your session has expired*; storage before/after; an orphan swept; a lock held by hand → 409
>   copy, never success; 360 px held; 7 367 log lines, **0 marker hits**. **It found two defects no
>   test did:** (a) the purge preview said *Would delete 0 session(s)*: `PurgePanel` read
>   `sessions_deleted`, which a dry run always reports 0 (AC-16; the count is `examined`), and the
>   test fake had `sessions_deleted: 3`, contradicting AC-16, so the suite was green — fake fixed
>   red-first (`cf65a91`), then the panel (`d3ea89b`); (b) every run's result appeared **~2.2 s after
>   a 32 ms response**, because `onSettled` returned its invalidations, so the mutation stayed
>   pending until `/health/ready` (2 s, 1.6's `probe_celery`) refetched — now fired, not awaited
>   (`4c7df0b`), guarded by `885fa1c` (reverting: 2 red). **`/verify`'s MINOR 1:** T2's lift had put an
>   untranslated `files.delete` raise inside the batch's per-session catch. That raise is reachable:
>   `_resolve_contained`'s `RuntimeError` fires on a symlinked shard. The catch recorded a deleted
>   session as failed and skipped its remaining files. The `try` now wraps only `delete_session`, so the
>   raise escapes the run as it did before (`49e2aef` RED → `5cfd637`). **A broad catch justified by
>   one `await` records the wrong fact once it covers two.** **Firefox not exercised.**
>
> - **`web-design-refresh`** (owner-initiated, outside the phases; **implemented 2026-10-26/27 on
>   `feature/web-design-refresh`; `/verify` PASS round 2; owner's T42 pass done; **merged 2026-10-27 as
>   PR #37, before OQ-17's 2026-10-30 floor by the owner's decision**, so Phase 4's gate, measured on
>   `/admin` until 2026-10-29, closes on the reskinned pages) — a **frontend-only reskin** from
>   the owner's Claude Design mockups plus one new static, lazy route, **`/welcome`**. No file outside
>   `web/`, two ADRs and the docs changes (AC-1). **ADR-0036**: fonts self-hosted from `@fontsource`
>   5.3.0 (Figtree 400–700, Bricolage Grotesque 500/700, JetBrains Mono 500), exact pins, latin only,
>   seven `@font-face`s declared in `index.css` (fontsource's own CSS would ship a `.woff` twin of each),
>   `.woff2` never inlined; `web/scripts/check-dist-hosts.mjs` ends `npm run build` and fails on any
>   third-party host in `dist/` (allow-list: www.w3.org, react.dev, reactrouter.com, github.com,
>   prosemirror.net, tailwindcss.com, localhost; Google Fonts refused by name; mutation recorded;
>   devops review applied: empty dist fails, `fileURLToPath`, `<img>/<source>/<iframe>` covered).
>   **ADR-0037**: colours, fonts and radii are `@theme` tokens (warm surfaces, brand-400, highlight,
>   success, danger; Tailwind's own `--radius-*` overridden, 10–20 px); `design.grep.test.ts` forbids
>   `style=`, colour literals and arbitrary colour classes in `.tsx` and keeps the fictional
>   illustration names under `features/marketing/` (ten mutations). **Shell**: wordmark `Link` inside
>   the one `h1`, *System status* moved to a `<footer>` with *How TailorCraft works*, the signed-in
>   nav becomes a fixed bottom bar below `sm` — **the same `<nav>`**, `NavLink`s with `aria-current`
>   (*Workspace* added; *How it works* for guests). **Intake**: numbered step rail (the third item is
>   an `<li>` outside the tablist), *Step n of 3* on mobile, dashed dropzone (`data-drag-over`),
>   native radios as a segmented control (`data-over-limit`), progress card, saved CVs as cards.
>   **Editor**: top bar (relocated back link, segmented tabs, save status), white sheet on page grey,
>   glyph toolbar with unchanged accessible names; **`ExportSheet`**, a disclosure that is inline at
>   `sm`+ and a bottom sheet below it around **the one** `ExportBar` (`max-sm:invisible` while closed,
>   never `hidden`); layout cards with the real WebPs; `TrackButton` card. **Board/history**: wells and
>   cards, *Move* pill on the native select; history still a `<ul>` with an `aria-hidden` header row.
>   **Auth**: one auth card; `/register` split with a **static** *Keep what you made* panel (no input,
>   button or link, pinned with a mutation, AC-35); `CheckYourEmail` step list. **Token-only**:
>   `/account`, `/admin/*`, 404 (class lines only, checked). **Two keyboard defects fixed red-first**:
>   `DocumentTabs` had no key handler, so the unselected document was unreachable by keyboard
>   (`5b90f21` → `95feb68`); `InputTabs` takes ArrowUp/Down (`f96e414` → `d1cac9f`). RED cycles:
>   nav (`2eb13fe` → `11d5394`), `ExportSheet` (`adc4e35` skeleton, `fa95310` → `312906d`),
>   `/welcome` (`57f3d1e` skeleton, `5bcfd3d` → `1a9a789`). **Edited pre-existing tests, each a pin or
>   markup follow:** `LayoutPicker.guards` ×2 (3.2's dependency-list pin gains the three font
>   packages; the `assetsInlineLimit` source pin now also requires `.woff2 → false`), `CvDropzone`
>   (class → `data-drag-over`), `JobPostingInput` (class → `data-over-limit` + danger token),
>   `BoardPage.a11y` (`lg:w-60` → `lg:w-64`), `AdminRoute.chunkFailure/chunkLoading` (import paths
>   after `LazyBoundary` moved to `components/ui/`), `InputTabs` (additions only: T13's RED cases,
>   T20's rail pins). **Measured (T41):** main `index-*.js` 145.79 →
>   **149.70 kB gzip (+3.91)**, over AC-39's 3 KB, **budget amended to 4 KB by the owner on
>   2026-10-27** (markup weight, no dependency); CSS 5.72 → 8.57 kB; `/welcome` chunk **3.39 kB**
>   (15), none of its text in main; fonts 112 428 B in 7 files, **78 576 B** fetched on a cold `/`;
>   contrast table computed from the tokens (slate-500 4.29 on `surface-sunk`, 3.89 on `page-grey`,
>   so never used there; no `text-slate-400`/`text-brand-500` anywhere); the guest-run request list
>   identical to `main`'s (AC-40, dev stack, one real-Gemini run each). **T42**, the manual pass on
>   the production bundle (Chrome **and Firefox**, 390 and 1280 px), was done by the owner on
>   2026-10-27 and reported fine; the findings are not itemised here. **`/verify`** (2026-10-27):
>   round 1 NEEDS CHANGES on one MAJOR, two soft REDs never hardened (the AC-30 `/welcome` privacy
>   tests, `ExportSheet` focus return). The mutations found the storage assertion **vacuous**: an
>   earlier render had already written the key, so both snapshots agreed. A `beforeEach` storage
>   reset fixed it (`334f3b2`). ADR-0037's table gained the shipped pairs (`cceb3dc`); the carried
>   MINORs were fixed (`5bab82c`): `danger-600` for the last three `red-700`s, 44 px signed-out and
>   footer links, the *Base CV* screen-reader label. Round 2 PASS. **Released 2026-10-27**: PR #37 (`a539040`, deploy run
>   40539367985). **Follow-up, released the same day:** PR #38 (`56fabc4`, run 40548606371),
>   `feature/admin-only-system-status` (owner's request) mounts the footer's *System status* **for
>   admins only** (RED 2 failed | 5 passed → GREEN). Everyone else gets no section and no
>   `/health/ready` poll; the endpoint stays public for the deploy. This amends AC-10. **New copy
>   outside the C-table, for the reviewer:** `HISTORY_COLUMN_LABELS` (*Role*, *Base CV*, *Requested*,
>   an `aria-hidden` header row). Follow-up named, not built: a CSP in nginx (`default-src 'self'`).
>
> - **`domain-fixcv-app`** (owner-initiated, outside the phases; **approved, implemented, verified and
>   released 2026-10-27**; PR #40, `8468b5f`, deploy run 40591221234) — production moves from
>   `cv.samolit.com` to the apex **`fixcv.app`**, which the owner registered the same day. **Config
>   only**: the app learns its origin from `PUBLIC_BASE_URL` alone (mail links, the `Origin` check,
>   the fetcher's `User-Agent`), so no domain, application, API, migration or frontend line changed.
>   **Traefik**: the main router answers ``Host(`fixcv.app`)``; a separate `tailorcraft-www` router
>   sends `www.fixcv.app` through the permanent `tailorcraft-to-apex` `redirectregex`, path and query
>   kept. One router per host, because Traefik requests one certificate per router. The compose `$$`
>   escape was read off a throwaway container (`^https?://[^/]+/(.*)$`, `https://fixcv.app/${1}`),
>   because `compose config` re-escapes its own output. **Smoke step**: `fixcv.app/health/ready` with
>   `--retry-all-errors` (a not-yet-issued certificate is curl exit 60, which plain `--retry` skips),
>   plus an exact `301 https://fixcv.app/health/ready?x=1` assertion on `www`. **OQ-1, owner:
>   `cv.samolit.com` is retired, not redirected.** It had already been NXDOMAIN at the authoritative
>   server when planning began (production unreachable by name), and the owner chose not to restore
>   it, since no customers were there. **ADR-0038**: the apex is the host; no `*.fixcv.app` name other
>   than the `www` redirect ever points at an HTTP server. The reviewer widened this from "our routers"
>   to **any DNS record**, including vendor CNAMEs and dangling ones. **ADR-0008 (m)**: **OQ-16 is
>   closed by that condition**, and the `__Host-` rename of `tc_refresh` is unscheduled until the
>   trigger fires. Two cookie docstrings no longer name a host. The mail sender stays
>   `notifications.samolit.com` (OQ-7). **`/verify`** round 1 PASS; its MINORs were fixed in `2e64751`
>   and the local runbook. The new *Changing the production host* runbook first said "roll back by
>   re-running the previous deploy", which here would have re-served a name that no longer resolves.
>   It now says to fix forward. **Released 2026-10-27, before OQ-8's 2026-10-30 floor, by the owner's
>   decision**: the box `.env` was edited and `check-settings` passed while the deploy waited, then the
>   owner approved. **Measured after release (2026-10-27):** `/health/ready` ready (Postgres, Redis,
>   Celery 1 worker); certificate `CN=fixcv.app`, SAN `fixcv.app`, issuer Let's Encrypt YR2; `www` →
>   `301 https://fixcv.app/health/ready?x=1`; a login `POST` with `Origin: https://cv.samolit.com` →
>   403. The owner reports "everything works fine" for the rest of the production checks (AC-8,
>   AC-10, AC-11). They are not itemised here, and AC-12's stranded counts, the Traefik version and
>   the `POST` redirect status were not recorded. Every user was signed out once. Phase 4's gate (to
>   2026-10-29) now spans a host change as well as the reskin.
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
