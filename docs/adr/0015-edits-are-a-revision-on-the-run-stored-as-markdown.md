# ADR-0015: Edits are a revision on the run, stored as Markdown, guarded by an aggregate-owned version

- **Status:** Accepted
- **Date:** 2026-09-13
- **Relates to:** ADR-0014 (the run is queued and polled; §9 kept `TailoredDocument` a value object
  and named the trigger that would promote it; its 2026-09-13 amendment §6 left a concurrent
  duplicate delivery open and named optimistic versioning as the fix — **this ADR closes it**),
  ADR-0004 (the LLM's output is untrusted text, sanitized by whoever renders it), ADR-0007
  (persistence conventions), ADR-0010 (the guest session owns the run). Supersedes nothing.
  Amends **Constitution §4.3**: the `tailoring` row no longer names `TailoredDocument` as an
  aggregate.

## Context

Slice 1.4 lets the user **edit** the tailored CV and cover letter before downloading them (FR-4,
US-4). That is the first time anything in the product *writes* a document after the model did, and
it forces five questions that 1.5 (export), 2.3 (history) and 2.4 (the guest→registered claim) will
all inherit. Answering them in a spec that dies with the slice would mean answering them three
times, differently.

1. **What is an edited document?** A new aggregate, or a state of the run? ADR-0014 §9 said the
   day a document gets an independent lifecycle is the day it becomes an aggregate, and pointed at
   1.4 as the slice most likely to be that day. The question has to be asked honestly here.
2. **What is stored?** TipTap's native document is ProseMirror JSON; the model's output is
   Markdown; a PDF wants HTML. Whatever is stored is a client-chosen document — untrusted input in
   exactly the sense the model's output is — and it must feed four export formats in 1.5.
3. **Who owns the version number?** ADR-0014's amendment named `version_id_col` as the fix for the
   duplicate-delivery race. The same number has to serve a second purpose now: an edit made against
   version 7 must not silently overwrite version 8.
4. **What does the write look like on the wire**, and what does the client get back?
5. **Where is the sanitizer?** G-33 in 1.3's failure contract pinned that LLM output is rendered as
   text until a renderer needs HTML. The editor is that renderer.

The forces: one owner (the guest session), one lifetime (the session's 24 h), a hard rule that no
business rule is re-implemented in TypeScript, and a learning goal that prefers the pattern that
teaches something true — here, that TipTap *is* ProseMirror, and that an aggregate owns the facts
it has rules about.

## Decision

### 1. An edit is a revision on `TailoringRun`, not a second aggregate

Two nullable columns (`edited_cv`, `edited_cover_letter`) and their whole-second instants, on
`tailoring_run`. The model's draft columns and their `CHECK`s are untouched: **the draft is never
overwritten**, and it is kept but not served in this slice. `revise_cv` / `revise_cover_letter`
are legal only from `succeeded`; `current_documents` returns the revision where one exists and the
draft otherwise, beside 1.3's `documents` (always the draft).

ADR-0014 §9's trigger was examined and **is not reached**: there is no history (one revision per
document, the latest replacing the previous), no per-document identity (1.5 addresses a document
by *kind*, `cv` or `cover_letter`, which is an address, not an identity), and no reuse across
runs. The invariants that matter — *editable only when `succeeded`*, *the draft is never
overwritten* — are rules *between* the run's status and its documents; on a second aggregate they
become the cross-aggregate rules §9 kept the documents on the run to avoid.

**The trigger, restated for the next slice that meets it:** a revision *history*, a document
shared across runs, or an export that needs a document id. Any one of those, and the promotion is
an additive migration copying four columns into a table. This paragraph is where to start.

### 2. Markdown is the one stored format, and 1.4 renders no HTML anywhere

The model already writes Markdown; the revision is stored as Markdown too, validated by **the same
value objects and the same bounds** the model's output is held to (`TailoredCv`, `CoverLetter`).
One format per row, zero new validators, zero new Python dependencies in this slice.

The document grammar is small and closed: paragraphs, headings, bullet and ordered lists, hard
breaks, bold, italic, and links. Anything outside it — raw HTML, tables, fenced code — is *text*,
on both sides of the bridge.

**Three obligations 1.5 inherits, stated now:** Markdown → HTML happens **on the server, once, at
render time** — `markdown-it-py` with `html=False`, then `nh3` restricted to the tags the grammar
can produce, then WeasyPrint with a `url_fetcher` that refuses every URL. The DOCX path walks the
same token stream. Neither needs a third format, which is the point of choosing this one.

### 3. The aggregate owns the version; the database only checks it

Every named transition on `TailoringRun` does `self._version += 1` (invariant TR-8, with a
table-driven test over every transition). The imperative mapping declares
`version_id_col=tailoring_run_table.c.version, version_id_generator=False`, so SQLAlchemy emits
`UPDATE … SET version = :new WHERE id = :id AND version = :loaded` and raises `StaleDataError` on
zero rows; the repository translates that into a domain `TailoringRunConcurrentlyModified`.

**One column, two races closed:**

- **The stale edit.** `PUT` carries `expected_version`; the aggregate refuses a mismatch with
  `TailoredDocumentVersionConflict` → **409 `document_version_conflict`** carrying
  `current_version`. A database-level race between two writers who both read the same version
  answers the same code with `current_version: null`.
- **ADR-0014 amendment §6's duplicate delivery.** Two deliveries of one `queued` run both read
  `queued`, both `mark_started`; the second `save` matches no row, and `ExecuteTailoringRun` step 4
  catches the conflict and returns `SKIPPED` **before paying**. The stale-run sweep tolerates a
  per-run conflict for the same reason (1.3's G-36 lost update is closed by it).

**The one thing to know about this shape:** with the generator off, the mapper's check only
*detects* a race if the application changed the version. A write path that forgets to bump has no
concurrency protection at all. That is why "every transition increments" is an invariant with a
test, not a convention.

### 4. `PUT` on the document sub-resource, `expected_version` in the body, 409 on conflict

`PUT /api/tailoring-runs/{id}/documents/{kind}` with `{"content", "expected_version"}` replaces
the current content of one document in full and answers **200** with the full run. **The
response's `tailored_cv` and `cover_letter` fields now mean the *current* document** (the
revision if any, else the draft); `version`, `tailored_cv_edited_at` and `cover_letter_edited_at`
are added to the response and to the list summaries. The draft is not served: nothing in this
slice reads it, and serving two bodies per document doubles the PII in every poll for a feature
that is not built.

Authorization is the link `tailoring_run.guest_session_id == session.id`, checked in the use case
through the composed read; "not mine" is **404**, as in 1.3. The value object is constructed in the
router, because it *is* the validation boundary.

### 5. The editor sanitizes by construction, and the limit of that argument is written down

Markdown → tokens (`markdown-it`, `html: false`) → ProseMirror nodes → `toDOM()`. **No HTML string
ever exists on the client**, so there is nothing for a sanitizer library to sanitize: text nodes
become DOM text nodes, and the only attributes rendered are `level` on a heading, `start` on an
ordered list, and `href` on a link. The first two are integers. **The third is the whole residual
risk**, and it is closed by the Link extension's protocol allow-list (`http`, `https`, `mailto`),
`openOnClick: false`, `autolink: false`, and a `javascript:` fixture in the tests.

**The invariant this argument rests on, written in the schema module:** no node or mark other
than `link` may carry a URL-valued or free-form attribute. An image node, a `data-*` attribute or
a `style` mark re-opens the question and needs its own allow-list and its own test. The HTML
path's sanitizer (`nh3`) is 1.5's obligation, handed on the way G-33 handed this one to 1.4.

## Alternatives

- **A `TailoredDocument` aggregate** — `(id, run_id, kind, draft, revision, version, edited_at)`.
  The tempting one, and ADR-0014 §9 half-expected it. Its genuine win: each editor tab gets its own
  version, so the two tabs never contend on one number and the client needs no save
  serialization. Its cost: a second table, repository, identity, `guest_session_id`, index,
  cascade, `version_id_col` and backfill; a cross-aggregate `succeeded ⇔ two documents` rule; a
  worker that writes to two aggregates on success. The client-side serialization it would remove
  is a TanStack mutation `scope` — one line. Rejected; the trade is not close.
- **HTML as the stored format.** TipTap-native load and save. Rejected: the draft is Markdown and
  the revision would be HTML — two formats in one row, every consumer branching on which — and it
  creates the HTML path (and its server-side sanitizer, a second copy of the grammar in Python)
  in the one slice that has no need for it.
- **ProseMirror JSON as the stored format.** Native to the editor. Rejected: a hand-written Python
  validator of node types, marks, attributes, depth and size, kept in step with the schema by hand,
  plus a Python renderer for four export formats, for a `JSONB` column nobody can read.
- **Plain text.** Rejected: it deletes headings and lists, the "layout styling" US-4 asks for, and
  1.5's PDF would be a wall of text.
- **SQLAlchemy's default version counter** (`version_id_col` alone). Closes the duplicate-delivery
  race with zero domain change. Rejected: the number is invisible to the domain, so the edit
  contract would be checked in the use case against a value the aggregate did not set, and every
  domain test of `revise_*` would need a database.
- **A hand-written `UPDATE … WHERE version =` or `SELECT … FOR UPDATE` in the repository.**
  Rejected: it re-implements what the mapper does for free, and `FOR UPDATE SKIP LOCKED` makes a
  locked run look `MISSING` (ADR-0014 amendment).
- **`If-Match` / ETag with 412.** The HTTP-native precondition, and the honest runner-up. Rejected
  because the client already holds `version` as a JSON field, the conflict body needs to carry
  `current_version` (a 412 with a body would be a novelty here), and the codebase's other conflict
  — `tailoring_already_running`, with an id in its body — is a 409. One shape for "your request
  contradicts the current state".
- **`PATCH`, or `POST …/revisions`.** Rejected: the request replaces one document's content in full
  (the definition of `PUT` on a sub-resource), and there is no revision collection to post into.
- **A client-side sanitizer library (DOMPurify) on top of the schema.** Rejected: there is no HTML
  string to hand it, and a sanitizer guarding nothing teaches that the schema argument is not
  trusted. The argument's one real hole (the URL attribute) is closed by name instead.
- **Serving the draft and a "reset to the AI draft" endpoint.** Deferred, not rejected: every
  extra endpoint is an authorization rule and three failure rows, and the columns make it a later
  one-liner.

## Consequences

**Easier:**
- 1.5 exports `run.current_documents` in four formats from one Markdown source, with the
  server-side sanitization chain already named.
- 2.3 (history) and 2.4 (claim) re-key or list one table, not two.
- Domain tests assert versions and revision rules with no database; the concurrency contract is a
  pure rule with a table-driven test.
- The 1.3 duplicate-delivery residual is closed as a side effect of a column the editor needed
  anyway.

**Harder, and watched:**
- **Both tabs share one version.** Saves are serialized per run on the client (mutation `scope`),
  and `expected_version` is read from the cache at send time. Two windows on one run will
  see 409s; that is correct, and the UI offers *Load latest / Keep mine*.
- **The response fields changed meaning.** `tailored_cv` and `cover_letter` are the *current*
  document. Every consumer that assumed "the model's draft" — there are none today — must read
  `*_edited_at` to know.
- **Markdown is a lossy round trip for anything outside the grammar.** A table or a fenced block
  the model happened to produce becomes text in the editor and stays text on save. Accepted; the
  prompt asks for neither.
- **The schema is the sanitizer.** Any new node, mark or attribute in the editor is a security
  review, not a styling change. The invariant is in the schema module; the reviewer looks for it.
- **A transition that forgets `_version += 1` is silent.** TR-8's table-driven test is the only
  thing that makes it loud; keep it exhaustive over every named transition, including new ones.
- **1.5's TXT export** must decide whether "plain text" is the Markdown itself or a marker-stripped
  rendering. Not decided here.
