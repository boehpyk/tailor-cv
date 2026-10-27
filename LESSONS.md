# Lessons — the long form

The full text of CLAUDE.md's Conventions and Infrastructure footguns sections, moved here verbatim
on 2026-10-26. CLAUDE.md keeps a one-line index of each rule; the evidence, commit ids and measured
numbers are here.

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
  **Reads retry, writes hold (3.3, ADR-0031).** A refused write is held until `Retry-After` and
  never re-sent by itself; a read may retry, and the UI says so. A deadline from one response is
  **component state derived during render** (`useErrorHold`: `(error, receivedAt)` adjusted when
  the error changes, then `useHold` with one effect for the tick and an exact-deadline timeout) —
  not a store, not the query cache, not a `useEffect` copying the error. A copy-only surface takes
  `useErrorHold(error).phrase`, never `holdDeadline(error, Date.now())` at render, which moves the
  deadline every render. Word clock times in the **browser's locale** (no forced `hourCycle`); tests
  pin `'en-GB'`/`'UTC'` on the pure function and match `\d{1,2}:\d{2}(\s?[AP]M)?` in components.
  **Fake timers vs TanStack (3.3).** Retry delays and notifies are `setTimeout`s: use
  `vi.advanceTimersByTimeAsync` (the sync form hangs, it does not fail). TanStack notifies observers
  through `setTimeout(0)`, which Vitest schedules **+1 ms** when created inside a tick, so flush
  with `advance(1)`, not `advance(0)` (`60fba47`). A helper that advances the clock (80 ms per
  `settle()`) makes "deadline − 1 ms" land after the deadline: advance to `deadline − Date.now() −
  1` (`148f4e1`). `onlineManager` is a module global — `afterEach(() =>
  onlineManager.setOnline(true))`. `userEvent.type` can hang under fake timers; use `fireEvent`.
  **The app's client turns `refetchOnWindowFocus` off (4.1, F-1).** A test on a hand-built
  `QueryClient` keeps TanStack's defaults and can pass where the app fails: AC-31's demotion test
  did, and only the manual pass found it. Build test clients with `createAppQueryClient()`
  (`web/src/queryClient.ts`) when the behaviour depends on a default. A query that must refetch on
  focus says so itself, and `'always'` if the cache is within the 30 s `staleTime`. TanStack v5's
  `focusManager` listens for `visibilitychange` on **`window`** — the event does not bubble, so a
  dispatch on `document` refetches nothing (`e58df70`). And v5 refuses a query function resolving
  `undefined`: a 204 probe returns a sentinel (`true`), or every success is an error.
  **A lazy route's main-bundle half imports nothing from its chunk's modules (4.1).** One import of
  `adminCopy.ts` from `AdminRoute` would have pulled the whole module into the main chunk; the
  fallback strings live in `lazyCopy.ts`. Check with a grep of `dist/assets/index-*.js`.
  **Descendant routes inside a lazy chunk use `useRoutes`, not `<Routes>` JSX (4.2).** react-router
  lives in the main chunk, and `<Routes>` pulls `createRoutesFromChildren` in with it: +0.69 kB gzip
  on the main chunk for three admin routes. Route objects through `useRoutes` cost +0.20 in all.
  **`removeQueries({ queryKey, exact: true })` keeps the key's children (4.3, `301c75a`).** 4.2's
  erase dropped the account's detail key exactly, and every content body nested under it stayed
  cached for `gcTime`. To drop a subtree, drop by prefix.
  **A test fake's 403 `account_disabled` signs the tab out (4.3, `e7f68c2`)**, by 4.2's design, and
  unmounts the page. To fail one section, answer 503.
  **Count a page's `<h1>` inside `main`:** App's shell renders `<h1>TailorCraft</h1>` too.
  **A page-level `findByText(/^Loading/)` matches the lazy route's Suspense fallback first** (also a
  `role="status"`), so it cannot fail. Wait until the item route has been called, then assert the
  page's own loading line inside `main` (`e7f68c2`).
  **A mutation's `onSettled` that returns invalidation promises keeps the mutation pending until
  they refetch (4.4, `4c7df0b`).** The purge panel's result waited ~2.2 s behind a 32 ms response,
  because one invalidation was `/health/ready` (2 s). Fire invalidations without returning them
  unless the UI must wait for fresh data; `885fa1c` holds the refetch open and asserts the result
  renders meanwhile.
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
- **A race test's seed uses the same `Clock` as the action under test** (4.2, `ab88358`). AC-9's
  seed registered its user with a private clock at 2026-10-11 while the disable ran on the
  `clock` fixture (2026-09-04), so `User.disable` refused, correctly, to disable a user before it
  existed, and the race was never staged. A domain time invariant turns a mismatched seed clock
  into a test that measures nothing.
- **An idle-in-transaction backend reports `wait_event_type = 'Client'`, not NULL** (4.2,
  `618af74`). It is waiting for its client's next statement. An overlap proof from
  `pg_stat_activity` asserts `state = 'idle in transaction'`, not an empty wait event.
- **A new database read in `require_user` reaches the shared-session harness** (4.2, `b1cc08f`).
  Every bearer route now reads Postgres before the rate limiter, so the `app` fixture's savepoint
  is open when a 429 is raised, and the refused request's rollback **expires every instance the
  test has loaded**: the next `run.version` read is a lazy load, `MissingGreenlet`. Read loaded
  values into locals before the requests (and `refresh` what a later request needs). Harness only;
  production has a session per request. AC-29 lists it as the one allowed edit to an existing test.
- **`AsyncSession.get` reaches the sync `Session.execute`, not `AsyncSession.execute`** (4.3,
  `f5b461f`). A fault injected at the async `execute` misses every repository `get`. Install it on
  the sync `Session.execute`, which both paths reach.
- **A router-level dependency duplicated by a handler parameter is an equivalent mutant** (4.3,
  `c2b83bb`). Every admin handler takes `actor: AdminDep`, so dropping the router's
  `require_admin` changes no dependency graph and no request. Pin `router.dependencies` itself.
- **AC-33's `tiptap`/`markdown` grep of the admin chunk hits chunk-filename imports** (4.3): React's
  runtime lives in the `tiptap` manual chunk, so every chunk names it. Grep for library
  identifiers (ProseMirror, markdown-it), not the word.
- **`commit-msg` rejects a subject over 72 characters only after `pre-commit` has run the full
  suite** (4.2). Count the subject before you commit, or pay four minutes to learn it is 74.
- **A test fake that disagrees with the API's real shape hides a UI bug** (4.4, T28). The purge
  preview's fake answered `sessions_deleted: 3`, though a dry run always reports 0 (AC-16) and the
  count is `examined`; the panel read the wrong field and the suite stayed green until the manual
  pass read *Would delete 0 session(s)*. Build a fake from a real response (or the schema), and pin
  its shape with a guard test (`cf65a91`).
- **Known flakes seen in 4.4, each passing on re-run:** AC-20's login event-loop fraction (0.908
  against 0.88, once); `test_me_tracked_applications…[control_character]` answering 401 once; the
  first admin Vitest test in a file timing out under load. Re-run before chasing; if one recurs,
  it is a finding.
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
- **A loading control that exists but is disabled satisfies `findByRole`** (3.2, `6cf4628`). The
  export buttons render disabled while the list loads, so `findByRole` resolved at once and the
  click sent nothing; five T24 tests were corrected before GREEN. Wait for enabled
  (`waitFor(() => expect(btn).toBeEnabled())`) before clicking.
- **A RED test can contradict the spec's step order** (3.2, `cc1bd0e`). Two cap tests seeded the cap
  with jobs on the very key they then requested, so the idempotent lookup (step 3) correctly
  answered before the cap could. Seed the cap with keys the request cannot match.
- **A test encodes what the code *should* do — never what it was observed doing.** A test written by
  running the code and recording the answer has no source of truth independent of the code, so it can
  never disagree with it. When an acceptance criterion and the implementation disagree, **fix one of
  them on purpose and say which won** — do not write the test that ratifies the accident. And a
  docblock claiming coverage the assertion cannot deliver is the same defect one level up: if a test
  says it guards a regression, verify it fails when you reintroduce that regression.

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
- **Under `pipefail`, never pipe into a reader that exits early** (`grep -q`, `head`). It closes the
  pipe on its first match, the writer dies of `SIGPIPE`, and the pipeline fails because it *found*
  the text. That happened to the release after 4.4 (run 38710525932): *no Referrer-Policy* on a
  config that had one. Capture first (`out="$(cmd 2>&1)"`), then `grep -q … <<<"$out"`, and print
  `$out` when `cmd` fails. Don't send its stderr to `/dev/null`.

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
- **WeasyPrint 70 crashes on `li::marker { content: … }`** with `TypeError: min-content width for
  TextBox not handled yet`, on any `<ul>` — found by rendering the whole corpus per layout, not by a
  unit test. Use `list-style-type: "\2013  "` (a string value) instead. Also: `text-transform` and
  `small-caps` change the **case of text extracted** from the PDF, and WeasyPrint's embedded
  `BaseFont` names are hyphenated (`Liberation-Serif-Bold`), so a read-back check compares
  case-insensitively and hyphen-tolerantly.
- **A migration adding a CHECK on a column the mapping does not write yet cannot land green alone**
  (3.2, `37c7424`). Every ORM PDF insert left the column `NULL` and hit
  `ck_export_job_layout_template_only_for_pdf`: 198 failures. The migration (T11) and the mapping
  (T12) are one commit. "Migration before mapping" (2.3) holds for a nullable column with no
  constraint; a constraint the mapping must satisfy ties the two together. **So does any constraint
  at all (4.1, `5d75124`):** the `test_autogenerate_against_head_produces_an_empty_diff` tests read a
  CHECK the metadata lacks as drift, even when every insert satisfies it via a `DEFAULT`.
- **Vite inlines any asset under 4 KB as a `data:` URI** (`assetsInlineLimit`). The 2 KB layout
  previews went into the JS chunk until `web/vite.config.ts` excluded `.webp`; AC-38 now asserts they
  are hashed files.
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
  passes vacuously. Descend, duck-typed (the class is private). **It also serves a copy:**
  `_IncludedRouter` builds `_EffectiveRouteContext`s from `route.endpoint`, so patching
  `route.dependant.call` on the original `APIRoute` is never invoked (4.1, `4c61a28`). A spy wraps
  `route.endpoint` and resets the effective-route cache; a test that counts calls asserts its
  positive control first.
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
  Parse `Set-Cookie` and re-attach it by hand in such a script. The same jar *stores* a `Secure`
  cookie set over `http://` and silently never sends it back, so since the `__Host-tc_guest` slice
  every test client's base URL is `https://testserver` (reverting it: 186 tests red).
- **Access logs record query strings by default** (4.2, T26). nginx's `combined` format logs
  `$request` and uvicorn's `uvicorn.access` logs the path with its query, so anything an operator
  types into a `GET` query (the admin search, `?q=<part of an email>`) becomes a log line. Found
  only by T26's manual log grep. Both now strip it on **every** route: a `logging.Filter`
  (`observability.StripAccessLogQuery`) installed by `configure_logging` in every process that
  imports the app, after uvicorn's own `dictConfig` (which replaces handlers, not a logger's
  filters) (`abef0a2`), and a `noquery` `log_format` (`$request_method $uri $server_protocol`) in
  both nginx configs (`f77b1d6`). **Traefik's access log, on the box, is unchecked.**
  **Accepted gap (owner, 2026-10-11): nginx's `error_log` still writes the full request line,
  query included** (`log_format` governs `access_log` only), e.g. every admin search during a 502
  window after an api restart. **Sentry was the third copy and is closed** (`9984003`):
  `strip_query_strings` runs as `before_send` **and** `before_send_transaction` (a transaction
  skips `before_send`), dropping the incoming `request.query_string` and the outgoing httpx /
  `http.client` `http.query`/`url` query on spans and breadcrumbs. The outgoing URL **path** still
  reaches Sentry.

