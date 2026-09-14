/**
 * Mirrors `TailoringRunResponse` / `TailoringRunSummary` / `TailoringRunListResponse`,
 * `CreateTailoringRunRequest` and `ReviseDocumentRequest` in the API's
 * `infrastructure/api/schemas/tailoring.py`, field for field, nullability included — plus the extra
 * keys the revision endpoint's 409 and 422 envelopes carry (`infrastructure/api/errors.py`).
 *
 * Hand-written for now, and that is a known seam (`features/intake/types.ts` and
 * `features/posting/types.ts` carry the same note): two declarations of one contract drift. When the
 * API surface grows past a couple of endpoints, generate these from the OpenAPI schema FastAPI
 * already publishes at `/openapi.json` rather than maintaining them by hand.
 *
 * Wire types, not view models: UUIDs are `string`, datetimes are ISO-8601 `string`s, and every
 * field is `readonly` because a response is a fact the server stated, not something the client edits.
 */

/**
 * Where a run stands. Mirrors the domain's `TailoringRunStatus` — a closed set of four, so a union
 * of literals rather than `string`: an unhandled member is a compile error, not a silent fallthrough.
 *
 * `queued` and `running` are the two non-terminal states the poller keeps polling through;
 * `succeeded` and `failed` are terminal and decided exactly once. **A `failed` run arrives as a 200**,
 * on the same polled resource as a `running` one — it is a recorded state of the run, not an HTTP
 * error, which is why the UI must tell the two apart by `status` and never by the response code.
 */
export type TailoringRunStatus = 'queued' | 'running' | 'succeeded' | 'failed';

/**
 * Why a run ended in `failed`. Mirrors the domain's `TailoringFailureReason` — nine values, closed.
 *
 * A component maps these to copy with an exhaustive `Record<TailoringFailureReason, …>`, so a tenth
 * reason added on the server becomes a TypeScript error at the one place that must write a sentence
 * for it, rather than a blank box in the browser.
 *
 * What these reasons do **not** decide here is whether "Try again" is offered. That is `retryable`,
 * below, and it comes from the API.
 */
export type TailoringFailureReason =
  | 'llm_unavailable'
  | 'llm_rate_limited'
  | 'llm_refused'
  | 'llm_timed_out'
  | 'llm_output_invalid'
  | 'inputs_too_large'
  | 'llm_error'
  | 'not_queued'
  | 'abandoned';

/**
 * The request body for `POST /api/tailoring-runs` — the whole of it.
 *
 * There is no model, prompt version, temperature or token budget, and the absence is the contract:
 * the client may ask for "tailor this CV against this posting" and nothing about how that is paid
 * for (the Pydantic model is `extra="forbid"`, so a stray field is a 422).
 */
export interface NewTailoringRun {
  readonly base_cv_id: string;
  readonly job_posting_id: string;
}

/**
 * Which of a run's two documents a request addresses. Mirrors the domain's `TailoredDocumentKind`
 * StrEnum — **the values are URL path segments** (`PUT /api/tailoring-runs/{id}/documents/{kind}`),
 * which is why they are the wire spelling (`cover_letter`, underscore) and not a camelCased view
 * name. An unknown kind is FastAPI's own 422 before any handler runs, so a union of literals here
 * makes that request unconstructable rather than merely rejected.
 */
export type TailoredDocumentKind = 'cv' | 'cover_letter';

/**
 * The request body for `PUT /api/tailoring-runs/{id}/documents/{kind}` — the whole of it
 * (`extra="forbid"`, so a stray field is a 422).
 *
 * `content` is the document as Markdown, replacing the current text **in full** — this is a `PUT`
 * on a sub-resource, not a patch and not an appended revision (ADR-0015 §1: no history).
 *
 * `expected_version` is the `version` the editor was shown when it loaded the text (optimistic
 * concurrency, ADR-0015 §3). The server compares, never the client: a stale number is a 409
 * `document_version_conflict`, and the length and character rules on `content` are the value
 * object's on the server — the client pre-validates for UX at most and re-implements nothing
 * (Constitution §4.5). `>= 1` on the wire, because a run is born at version 1; the type does not
 * say so, because the only honest source for this number is a `TailoringRun` you already hold.
 */
export interface ReviseDocumentBody {
  readonly content: string;
  readonly expected_version: number;
}

/**
 * Why a 422 `document_invalid` refused the text — the `problem` key of that error's envelope.
 * Mirrors the four fixed labels `errors.py` writes for the value object's four failures (E-10 …
 * E-12). Closed, so the copy that names the floor or the ceiling is an exhaustive `Record` and a
 * fifth label is a compile error rather than a blank notice. **The message never carries the
 * text**, and neither does this.
 */
export type DocumentProblem = 'empty' | 'too_short' | 'too_long' | 'invalid_characters';

/**
 * The extra keys the revision endpoint's rejections carry **besides** `code` and `message` — what
 * `ApiError.details` holds for each code, written down so a reader can narrow *towards* a known
 * shape.
 *
 * These are documentation of the wire, not a cast target. `ApiError.details` is
 * `Record<string, unknown>` on purpose, and the reader that acts on a key narrows it with `typeof`
 * at the point of use, exactly as `activeTailoringRunId` does for `tailoring_already_running`'s
 * `active_tailoring_run_id` (`apiErrorCopy.ts`). `as DocumentVersionConflictDetails` would assert
 * a shape the server never promised to a client that did not check.
 */

/**
 * 409 `document_version_conflict` — the editor's `expected_version` was stale (E-8), or two writers
 * raced and the second `UPDATE` matched no row (E-9).
 *
 * `current_version` is the number the run is actually at for E-8, so the client can refetch and
 * compare; it is **`null`** for E-9, because the copy the server held *was* the stale one and the
 * true number lives in a row it has just been told it does not have. Either way the client's next
 * move is the same — re-read the run — and the refetch, not this field, supplies the version to
 * save against.
 */
export interface DocumentVersionConflictDetails {
  readonly current_version: number | null;
}

/**
 * 409 `tailoring_run_not_editable` — the run is `queued`, `running` or `failed` (E-7). `status`
 * is carried so the client can tell "still working, keep polling" from "there is nothing to edit"
 * without a second read. An honest client never sends this request: the editor is shown only on
 * `succeeded`.
 */
export interface TailoringRunNotEditableDetails {
  readonly status: TailoringRunStatus;
}

/** 422 `document_invalid` — the value object refused the text (E-10 … E-12). */
export interface DocumentInvalidDetails {
  readonly problem: DocumentProblem;
}

/**
 * One run in full, as `POST /api/tailoring-runs` (202), `GET /api/tailoring-runs/{id}` (200) and
 * `PUT /api/tailoring-runs/{id}/documents/{kind}` (200) return it — including both tailored
 * documents.
 *
 * Every field from `tailored_cv` down to `llm_duration_ms`, plus `started_at` / `completed_at`, is
 * `null` while the run is `queued` or `running`. That is the polling contract: the client re-reads
 * this same shape until `status` is terminal.
 *
 * **`tailored_cv` and `cover_letter` are the *current* document** (ADR-0015 §4): the visitor's
 * revision where one exists, else the model's draft. The draft is never served alongside a
 * revision — nothing in 1.4 reads it, and two bodies per document would double the PII in every
 * poll. `tailored_cv_edited_at` / `cover_letter_edited_at` say which one you are reading.
 *
 * `prompt_tokens` and `completion_tokens` are deliberately absent on the server — cost accounting,
 * nothing a user can act on — so they are absent here too. Do not "complete" this type from the
 * database columns.
 */
export interface TailoringRun {
  readonly id: string;
  readonly status: TailoringRunStatus;
  readonly base_cv_id: string;
  readonly job_posting_id: string;

  readonly failure_reason: TailoringFailureReason | null;

  /**
   * Whether "Try again" is worth offering. **Computed by the API from `failure_reason`; the client
   * reads it and never re-derives it** (AC-13, Constitution §4.5). "Which failures are worth paying
   * for again" is a business rule, and a TypeScript copy of it would be a second authority that
   * drifts. `false` for a run that has not failed, which is not a claim that it is unretryable —
   * read it only when `status === 'failed'`.
   */
  readonly retryable: boolean;

  /**
   * The **current** tailored CV as Markdown — the visitor's revision if one exists, else the
   * model's draft (ADR-0015). Untrusted either way: render it as text, never as HTML (AC-31).
   */
  readonly tailored_cv: string | null;
  /**
   * The **current** cover letter as Markdown — the visitor's revision if one exists, else the
   * model's draft (ADR-0015). Untrusted either way: render it as text, never as HTML (AC-31).
   */
  readonly cover_letter: string | null;
  /** Of the current text, whichever it is. */
  readonly tailored_cv_character_count: number | null;
  /** Of the current text, whichever it is. */
  readonly cover_letter_character_count: number | null;

  /** Provenance: what actually produced these words. `null` until the call succeeds. */
  readonly model: string | null;
  readonly prompt_version: string | null;
  readonly llm_duration_ms: number | null;

  readonly requested_at: string;
  readonly started_at: string | null;
  readonly completed_at: string | null;

  /** The **guest session's** expiry — the session owns the 24-hour promise (ADR-0006). */
  readonly expires_at: string;

  /**
   * The optimistic-concurrency token (ADR-0015 §3). Send it back as `expected_version` on every
   * `PUT`; a stale one is a 409 `document_version_conflict`. Born at `1`, and **every** named
   * state change on the run increments it — the worker's as much as the visitor's — so a `succeeded`
   * run is already past 1 before anyone has typed, and the number is the run's, not the document's:
   * both tabs share it. Never `undefined` on the wire, and never guessed on the client.
   */
  readonly version: number;

  /**
   * When the visitor last revised the tailored CV. **`null` means "still the model's draft"** —
   * this, not a second body, is how the client knows which text it holds.
   */
  readonly tailored_cv_edited_at: string | null;
  /** When the visitor last revised the cover letter; `null` means "still the model's draft". */
  readonly cover_letter_edited_at: string | null;
}

/**
 * One run as it appears in a list: `TailoringRun` **minus the two document bodies**.
 *
 * Written out rather than `Omit<TailoringRun, 'tailored_cv' | 'cover_letter'>`, for the reason the
 * server's `TailoringRunSummary` is not a subclass of `TailoringRunResponse`: a derivation makes the
 * *next* field added to the full shape appear in the list type by default, and the direction that
 * default leaks is a stranger's rewritten CV. Here it would only be a type that lies about the wire,
 * but a type that lies is how a component comes to read a field that is never there.
 *
 * `version` and the two `*_edited_at` instants ride here too (ADR-0015 §4): the workspace's
 * latest-run card and the run page must agree on the version without a second read, and "edited"
 * is a fact a list may state without carrying a word of the text. The character counts are of the
 * *current* document, as on the full shape.
 */
export interface TailoringRunSummary {
  readonly id: string;
  readonly status: TailoringRunStatus;
  readonly base_cv_id: string;
  readonly job_posting_id: string;

  readonly failure_reason: TailoringFailureReason | null;
  /** Computed by the API — see `TailoringRun.retryable`. */
  readonly retryable: boolean;

  readonly tailored_cv_character_count: number | null;
  readonly cover_letter_character_count: number | null;

  readonly model: string | null;
  readonly prompt_version: string | null;
  readonly llm_duration_ms: number | null;

  readonly requested_at: string;
  readonly started_at: string | null;
  readonly completed_at: string | null;
  readonly expires_at: string;

  /** See `TailoringRun.version` — the same token, so both surfaces agree on it. */
  readonly version: number;
  /** `null` means "still the model's draft" — see `TailoringRun`. */
  readonly tailored_cv_edited_at: string | null;
  /** `null` means "still the model's draft" — see `TailoringRun`. */
  readonly cover_letter_edited_at: string | null;
}

/** Every run a guest session owns, newest first. `items` is `[]` for a session with none. */
export interface TailoringRunListResponse {
  readonly items: readonly TailoringRunSummary[];
}

/**
 * Whether a run is still in flight — `queued` or `running` — and so worth polling.
 *
 * This restates the server's own documented polling contract (the two non-terminal states of
 * `TailoringRunStatus`), not a business rule the client invents. It lives here, once, so the poller
 * and the panel that decides "is this the working state" cannot disagree about which statuses count.
 * Written as a `switch` over the union so a fifth status is a compile error rather than a run the
 * poller silently abandons.
 */
export function isActiveTailoringRunStatus(status: TailoringRunStatus): boolean {
  switch (status) {
    case 'queued':
    case 'running':
      return true;
    case 'succeeded':
    case 'failed':
      return false;
  }
}
