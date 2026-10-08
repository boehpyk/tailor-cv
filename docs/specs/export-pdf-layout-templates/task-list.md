# Task List: export-pdf-layout-templates

> Ordered, small tasks in canonical order. Each should be reviewable in < 5 minutes and is one commit
> on `feature/export-pdf-layout-templates` (cut from `main` at `bfb98a9`). Run `make check` before
> each commit. Check off as you go.
>
> **Tiered TDD** ([sdlc.md §2](../../sdlc.md)). A task marked **RED** is written *before* the
> implementation it describes and must be observed failing **on its assertion** — never on an
> `ImportError`. Each **SKELETON** gives it real signatures with `NotImplementedError` bodies. Paste
> the recorded failure into the RED commit body (`TDD_RED=1 git commit` / `make check.static`, never
> `--no-verify`). A RED commit is never the last on the branch.
>
> **Every commit is green** (CLAUDE.md, *"Commit order is part of the plan"*). Where an obvious order
> would not be, the task says what it does instead.
>
> **Traps this slice sets for its own tests, named before anyone falls in:**
> - **Three signatures widen, and every stub, fake and seed breaks with them** (2.4's lesson; mypy
>   cannot see through `monkeypatch`). `ExportJob.request(…, layout_template=…)` is called in **14 test
>   files**; `find_latest_for_key` and `render` are faked in 3 more and delegated by
>   `infrastructure/tasks/container.py`'s wrapper. The mechanical update (`layout_template=
>   LayoutTemplate.CLASSIC` for a PDF seed, `None` for DOCX; forward the argument in every fake) lands
>   **in the skeleton's commit, by `qa`**, with no assertion changed in meaning. `grep -rn
>   "ExportJob.request(\|find_latest_for_key\|def render(" api/` before and after.
> - **Raw-SQL seeds** in `test_export_job_repository.py` and `test_purge_database.py` insert
>   `export_job` rows; after the migration a PDF row without `layout_template` violates the new CHECK,
>   and before it the column does not exist — so T11 fixes them **in the migration's own commit**.
> - **The migration lands before the mapping** (T11 before T12): a mapped column the table lacks
>   breaks every load (2.3's lesson).
> - **Golden digests are taken from `main` before anything moves**: `md`/`txt` corpus output (AC-10),
>   the 1.5 `STYLESHEET` (AC-14) and `wrap_in_document` (AC-16) — T9 records them while they are still
>   `main`'s.
> - **A skeleton satisfies every absence assertion** ("no row", "no event", "not called"). Pair each
>   with a discriminating positive control.
> - **From 2.4:** a refusal's skeleton is a **no-op**, so the RED goes red on `DID NOT RAISE`, not on
>   `NotImplementedError`; and **`NotImplementedError` subclasses `RuntimeError`** — assert exact types.
> - **A spy installed before setup records the setup** (2.5): the fetch-body spy in the picker tests
>   is cleared after the first render.
> - **`gcTime: 0` tests the garbage collector**; use `gcTime: Infinity` for absence assertions.
> - **The privacy test is hard from the start** (2.2's `/verify`): every step a status assertion.
> - **One suite at a time**; scope every count to ids the test created.
> - **`make test file=` can fail at collection** for a lone integration file (mappings load in a
>   fixture) — select with `k=`. **`make web.test file=` ignores `file=`.**
>
> **Status: approved 2026-10-08 (T0) — as written, every OQ on its recommendation.**

## Approval and ADRs

- [x] **T0** (human): approve the spec and plan; answer the gating OQs (OQ-1, OQ-2, OQ-3, OQ-9) and
      confirm or change the reversible defaults. *(Done 2026-10-08: approved as written; OQ-1 job, OQ-2 PDF only, OQ-3 Classic/Modern/Formal, OQ-9 build in parallel, `__Host-tc_guest` merges first — AC-44.)*
- [x] **T1** (`/adr`): **ADR-0030** — a PDF layout is a closed set of checked-in stylesheets, chosen
      per export request and recorded on the job (plan §9), **and** the Constitution §4 `export` row,
      in one commit. Public on landing.
- [x] **T2** (`/adr`): **ADR-0016 amendment** (the key, staleness unchanged, `layout_template`, caps,
      (d)'s arithmetic) and **ADR-0017 amendment** (one constant per layout, the port's keyword and
      docstring, the widened grep, system fonts only, §7 not pulled).

## Domain (`domain-modeler` · `qa`) — RED first

- [x] **T3 SKELETON** (`domain-modeler`, `qa` for the mechanical part): `domain/export/` —
      `LayoutTemplate` (complete: a closed enum has no body to defer), `DEFAULT_LAYOUT_TEMPLATE`,
      `ExportFormat.takes_layout_template` (`raise NotImplementedError`), `ExportJob.request` gains the
      **required** `layout_template` keyword and stores it (**no refusals yet** — no-ops), the
      `layout_template` property, `LayoutTemplateRequired`, `LayoutTemplateNotApplicable`,
      `ExportRequested.layout_template`. The use case passes `layout_template=None if format is DOCX
      else LayoutTemplate.CLASSIC` **temporarily** (commented `# T10 replaces this`), so the app stays
      green. `qa`: the 14 test files' seeds (trap 1). No assertion changed in meaning.
- [ ] **T4 RED** (`qa`): domain unit tests — **AC-1…AC-4** (the exact member set and order with the
      pin message; `takes_layout_template` for all four formats; the three refusals and their order,
      before `cls()`, no event — with a happy-path positive control; the property and the event field;
      no transition touches the layout).
      - Recorded red (10 red, 17 green; `tests/unit/export/test_export_job_layout.py`):
        `E   Failed: DID NOT RAISE LayoutTemplateRequired` (pdf + None);
        `E   Failed: DID NOT RAISE LayoutTemplateNotApplicable` (docx + each of 3 layouts);
        `E   InvalidRunVersion: run_version must be >= 1, got 0 (XJ-8)` (order: Required / NotApplicable
        must precede the run-version check, x2); `E   NotImplementedError` (x4, `takes_layout_template`
        is a property whose skeleton raises — unavoidable, no assertion reachable).
- [x] **T5 GREEN** (`domain-modeler`): implement `takes_layout_template` (a `match` closed by
      `assert_never`) and the two refusals in `request`. No test edited.
- [x] **T6** (`domain-modeler`, `api-dev`, `qa`): the two port signatures —
      `find_latest_for_key(…, layout_template)` and `render(…, layout_template)` — with the port
      docstring corrected (*"no template"* → *"no stylesheet"*, plus the paragraph of plan §0.6).
      **Threaded, not yet honoured:** the repository accepts and ignores the argument (the column does
      not exist until T11–T12), the renderer accepts it and renders Classic. Each says so in a
      `# T12/T13 honours this` comment; the reviewer checks both comments are gone by T13. `qa`:
      forward the argument in the 3 fakes and the `tasks/container.py` wrapper. **No RED partner** — a
      Protocol has no behaviour.

## Application (`domain-modeler` · `qa`) — RED first

- [x] **T7 SKELETON** (`domain-modeler`): `RequestExportCommand.layout_template: LayoutTemplate |
      None = None` (unused); `RenderExportJob` and `RenderDocumentInline` unchanged. Signatures only.
- [x] **T8 RED** (`qa`): **AC-5…AC-9** — real Postgres for `ExportJobRepository` **through an
      in-memory key-honouring fake for AC-6** (the SQL adapter ignores the layout until T12; the fake
      is dropped from AC-6's parametrization at T12 when the real adapter takes over), fake
      renderer recording `layout_template`, fake queue. Default applied; explicit recorded; DOCX +
      layout → no row, no event; the key widened; caps across layouts (repeat at cap returns the job);
      the worker passes the row's layout once; inline passes `None`.
      - Recorded red (11 failed, 12 passed; `tests/integration/export/test_export_layout_use_cases.py`):
        `AssertionError: assert <LayoutTemplate.CLASSIC> is <LayoutTemplate.MODERN>` (explicit layout
        / worker row layout / failed-job-of-that-layout, also `...FORMAL`); `Failed: DID NOT RAISE
        LayoutTemplateNotApplicable` (docx + each of 3 layouts); `assert False is True`
        (`created` on a different layout at the same version); `TooManyExportJobs` escaping a repeat
        request at the guest/user cap. Green on arrival (regression guards): default on a PDF, DOCX key,
        caps layout-blind, inline passes None. Uses fakes, not Postgres (existing style); T12 adds the SQL key.
        T9 committed first (4c4e751) so every commit is green.
- [x] **T9** (`qa`): **golden digests from `main`'s behaviour, before anything moves** — `md`/`txt`
      output per corpus document (AC-10), `sha256(pdf.STYLESHEET)` (AC-14), `wrap_in_document` for
      both documents (AC-16) — written as passing tests against the current code with the digests
      inline and a comment naming `bfb98a9`.
- [x] **T10 GREEN** (`domain-modeler`): `RequestExport` resolves the default and passes the layout to
      the lookup and the constructor; `RenderExportJob` passes `job.layout_template`;
      `RenderDocumentInline` passes `None`; T3's temporary line removed. No test edited.

## Infrastructure (`api-dev` · `qa`) — test-after, except the HTTP contract

- [x] **T11** (`api-dev`, `qa`): the Alembic revision (plan §0.12, §5) — read every line of the
      draft; the back-fill and the downgrade refusal are hand-written. **Lands before the mapping.**
      **The same commit** fixes the two raw-SQL seeds (`test_export_job_repository.py`,
      `test_purge_database.py`) to write `layout_template = 'classic'` on PDF rows: before the
      migration the column does not exist, after it a PDF row without it violates the CHECK, so no
      other order is green. The commit body says so (CLAUDE.md, *"reorder on purpose and say so"*).
- [x] **T12** (`api-dev`) *(Landed with T11 as one commit, `37c7424`: with the CHECK in the migration every ORM PDF insert is refused until the mapping writes the column — 198 failures — so no commit holding T11 alone is green. AC-6/AC-5 now also run against the SQL adapter in `test_export_layout_sql_repository.py`; the fake-backed tests stay.)*: `LayoutTemplateType`, the mapped column and the CHECK in the `Table`,
      `find_latest_for_key`'s `is_not_distinct_from` (T6's comment removed). Drop T8's in-memory fake
      from AC-6's parametrization (the real adapter now honours the key).
- [x] **T13** (`api-dev`): `infrastructure/export/layouts.py` — `CLASSIC_STYLESHEET` (1.5's, moved
      verbatim), `MODERN_STYLESHEET`, `FORMAL_STYLESHEET` within plan §0.7's bounds, `stylesheet_for`;
      `pdf.render_pdf(…, stylesheet=…)`, `STYLESHEET` removed and the module docstring rewritten; the
      renderer's PDF branch and `LayoutTemplateMissing`; `layout_template` on the three log lines (T6's
      comment removed). `test_pdf_constants.py`'s two greps iterate `LayoutTemplate` (mechanical).
- [x] **T14** (`qa`, after): persistence and migration — **AC-11…AC-13** (column, CHECK by name,
      back-fill on seeded 1.5-shape rows inserted *before* upgrading, up/down/up, the downgrade
      refusal with a Formal row and its clean path, unknown stored value → `ValueError`, the lookup's
      `None` semantics, `EXPLAIN` on the run index with other runs' rows seeded first).
- [x] **T15** (`qa`, after): the renderer — **AC-14…AC-20** (classic digest; the forbidden-token
      grep over every layout and the family allow-list; the identity seam and the AST no-interpolation
      test; the 1.5 security tests parametrized by layout with the socket patch; real-WeasyPrint
      read-back of text order and embedded `BaseFont` family per layout × document; the corpus × layout
      success run; the log fields). **AC-10**'s `md`/`txt` digests still green.
- [x] **T16 SKELETON** (`api-dev`): `CreateExportRequest.layout_template` and
      `ExportJobResponse.layout_template` (schemas are written whole — a field list *is* its
      signature); `to_response` sets `layout_template=None` (a placeholder), the handler does **not**
      yet pass the body's layout, and `LayoutTemplateNotApplicable` is **not** yet in the error map.
- [x] **T17 RED** (`qa`): API tests — **AC-21…AC-24, AC-26** for **both twins** (202/200/`Location`;
      omitted → `classic`; 422 `validation_error` for unknown ids without echo; 422
      `layout_template_not_applicable` for DOCX; no row on any refusal, counted; per-layout
      idempotency; `layout_template` on list and poll, `null` for DOCX; `current` after an edit and
      after a switch; `no-store` on `/api/me/` refusals; the download filename and headers unchanged).
      - Recorded red (`f1b0859`, 38 failed / 164 passed): `assert None == 'modern'` / `assert None == 'classic'` (response layout_template, both twins); `assert 202 == 422` (DOCX + layout, error unmapped); `assert 200 == 202` (switch to formal returns the classic job); list/poll layout map `{id: None,…} != {id: 'classic',…}`.
- [x] **T18 RED** (`qa`): **AC-37** (planted-marker test, guest and account, every layout, claim,
      history delete, erasure; red on its positive control — the render line's `layout_template` —
      before T19) and **AC-25** (the AST pin: `layout_template` read only in `_export_handlers.py`;
      the transfer-route set unchanged).
      - Recorded red (`078cfbe`, 3 failed / 3 passed): `AssertionError: the published ExportRequested for <job> carries <LayoutTemplate.CLASSIC: 'classic'>, not the chosen 'modern'` (guest and account); `AssertionError: request_export never reads body.layout_template`. AC-25's absence + delegation checks and the transfer-route set are green on arrival.
- [x] **T19 GREEN** (`api-dev`): thread `body.layout_template` into the command in the shared handler;
      `to_response` from the job; `LayoutTemplateNotApplicable` → 422 in
      `infrastructure/api/errors.py`. No test edited.
- [x] **T20** (`qa`, after): **AC-27** (OpenAPI enum on request and response) and **AC-28** (purge,
      history-entry deletion, erasure + dry-run counts, orphan sweep, claim — one PDF per layout plus a
      DOCX, real filesystem on `tmp_path`; and the empty diff over the retention and claim modules).

## Frontend (`react-dev` · `devops` · `qa`) — RED for behaviour, after for structure

- [x] **T21** (`devops`): `make layout.previews` — `api/scripts/render_layout_previews.py` (real
      renderer, synthetic fixture) + a throwaway-container rasterize/encode step; commit the three
      WebP files and `api/tests/fixtures/layout_previews.json`; document it in
      `docs/infrastructure.md`. The owner looks at the three pictures before T24.
- [x] **T22** (`react-dev`): types (`LayoutTemplate`, `ExportJob.layout_template`,
      `NewExport.layout_template?`), `features/export/layouts.ts` with the previews imported, the
      fixtures' jobs gain `layout_template` (mechanical). Confirm `RunPage` survives a CV ↔ cover-letter
      tab switch (it decides where `chosenLayout` lives — plan §0.10) and say which in the commit body.
- [x] **T23 SKELETON** (`react-dev`): `LayoutPicker` shell (renders the fieldset and legend, radios
      unwired), `defaultLayoutFor` (`throw new Error('not implemented')`), `ExportTarget.layoutTemplate`
      threaded but **not** matched in `latestExportJobFor`, `RunPage` state and props wired,
      `ExportBar` sends no `layout_template` yet.
- [x] **T24 RED** (`qa`): Vitest + RTL by role and text — **AC-30…AC-33** (legend and three radios;
      derived pre-selection from the newest PDF of either document, else Classic; the override shared
      across tabs; nothing in browser storage; the PDF control per selected layout through idle /
      queued / rendering / ready / stale / failed; switching never cancels or re-requests; the four
      states incl. `aria-busy` while loading and no jump; the preview `onError` fallback; the body
      carries `layout_template` for PDF only).
      - Recorded red: 28 of 34 failures are assertions: `TestingLibraryElementError: Unable to find
        role="radio" and name /Modern/` (also /Formal/, /Classic/; the skeleton renders a fieldset and
        no radios); `Unable to find role="button" and name /^Download PDF — Modern layout/`;
        `AssertionError: expected [] to have a length of 1 but got +0` (the PDF body assertions);
        `expect(element).toBeEnabled()`; `AssertionError: expected 'modern-job' to be 'classic-job'`
        (`latestExportJobFor` ignores layout); `expected 'ready' to be 'queued'`; `expected
        'requesting' to be 'idle'`; `expected 'downloadFailed' to be 'queued'`; `expected {…} to be
        undefined`. The other 6 are `Error: not implemented`, all `defaultLayoutFor` direct tests
        (a throwing stub cannot return a value; acceptable for those only). Totals: 34 failed, 1152
        passed. Passing on the skeleton by design (regression guards): single-layout queued / rendering
        / stale / failed bar tests and the md/txt no-layout GET.
- [x] **T25 GREEN** (`react-dev`): implement `defaultLayoutFor`, the target matching, the picker, the
      copy and the PDF request body. No test edited.
- [x] **T26** (`qa`, after): **AC-29** (the mirror pin naming the Python file), **AC-34** (a11y:
      arrow keys, `aria-describedby`, `alt=""`, *Selected* text, focus ring, 360 px wrap with no
      horizontal scroll), **AC-35** (no new dependency; `security.grep` untouched), and **AC-38**'s
      frontend half (the images are hashed assets, not inlined; `loading="lazy"`, `width`/`height`).

## Measure, release, document, verify

- [x] **T27** (`qa`, `devops`): **in the production image** (`docker build --target production`, its
      own tag): AC-18 and AC-19 as `slow` tests (fonts and libraries per layout — L-22, L-23), then
      **AC-39** — per layout × document `render_duration_ms` p95 (n = 40), the 20 000-character CV,
      `POST` → `ready` p95, fixture PDF sizes. Numbers written into the spec.
      Done 2026-10-08: AC-18/19 110 passed in the image; every p95 inside budget (feature-spec AC-39/40).
- [x] **T28** (`devops`): release impact — one migration (`down_revision 7e43a47327ec`), no package,
      container, queue, volume, beat entry or setting; re-read `deploy.yml`'s order (stop worker/beat →
      web → api → migrate → worker/beat) with line numbers; production `export_job` row counts and the
      uploads volume size (ADR-0016 (d)) in a read-only transaction; the release windows L-35/L-36
      noted in the PR.
      Done 2026-10-08: one migration `b10d1c777b0a` (down `7e43a47327ec`); `git diff --stat bfb98a9`
      over docker/, compose files, pyproject/uv.lock, web package files, .github/, settings, .env.example
      is empty; the only touched build files are `Makefile` (+`layout.previews`, dev-only) and
      `web/vite.config.ts` (`assetsInlineLimit` for .webp). `deploy.yml`: pull api worker beat web l.104;
      stop worker beat l.126; web l.129; api l.130; nginx force-recreate l.138; `alembic upgrade head`
      l.140; worker beat l.142. Production (host `hetzner`, read-only): `alembic_version` =
      `7e43a47327ec`; `export_job` **3 rows, 2 pdf** (backfill + ADD CONSTRAINT take milliseconds; no
      need to split the revision); uploads volume `tailorcraft_uploads` **1.7 MiB**. L-35 (new bundle
      -> old API: 422 on `layout_template` for seconds, Try again works) and L-36 (new API -> old
      schema: export reads 503 for seconds) are accepted, R-3 — note both in the PR.
- [x] **T29** (docs): **AC-43** — CLAUDE.md (status block incl. the stale 3.1 line, the port change,
      `make layout.previews`, any footgun met), `docs/roadmap.md` (3.2, 3.1 status corrected, OQ-9's
      answer), `docs/infrastructure.md`, FORboehpyk.md.
- [ ] **T30** (owner): **AC-44** — confirm the `__Host-tc_guest` slice is merged to `main` (or the
      trigger is re-armed with a reason); quote the answer in the PR description.
- [ ] **T31** `/verify` → reviewer PASS (zero CRITICAL, zero MAJOR); every acceptance criterion checked;
      **every RED task carries a recorded failure**; **AC-36**'s diff empty; **AC-40** (bundle and
      `POST` p95) and **AC-42** (suite green twice) recorded; a manual pass on `:8080` with real
      Gemini — tailor as a guest, export each layout and the DOCX, open each PDF by eye against its
      preview, switch layouts mid-render, edit and see every layout go stale, claim, export as the
      user, delete the history entry; the logs grepped for the planted text (0 hits) with a positive
      control.

## Notes for `/verify` (gathered during `/implement`)

- T19 (`5cbbff6`) also touched `routers/me_tailoring_runs.py` (plan §3 said `_export_handlers.py` was the
  only router file): a `_refusal_no_store` translate wrapper so the account POST-export refusals carry
  `no-store` (AC-24). Side effect: every handler-raised refusal on that route now has it. **Gap left:**
  the account 429 from the rate limiter is raised in the shared handler, not via `translate`, so it has
  no `no-store`.
- T14's migration tests were not mutation-tested.
- T11+T12 landed as one commit (`37c7424`); T9 before T8 (`4c4e751`) — both reordered to keep every commit green.
