/**
 * Mirrors `BaseCvResponse` / `BaseCvListResponse` in the API's
 * `infrastructure/api/schemas/intake.py`, field for field.
 *
 * Hand-written for now, and that is a known seam (see `api/health.ts`'s docstring): two
 * declarations of one contract drift. When the API surface grows past a couple of endpoints,
 * generate these from the OpenAPI schema FastAPI already publishes at `/openapi.json` rather than
 * maintaining them by hand.
 */

/**
 * Where a `BaseCv` stands on the one thing this slice tracks: has extraction been decided.
 * Mirrors the domain's `BaseCvStatus` (`domain/intake/value_objects.py`) — a closed set, so a
 * union of literals rather than `string`: an unhandled member is a compile error here, not a
 * silent fallthrough at runtime.
 */
export type BaseCvStatus = 'uploaded' | 'extracted' | 'extraction_failed';

/**
 * Why extraction failed, when `status === 'extraction_failed'`. Mirrors the domain's
 * `ExtractionFailureReason`. `failure_message` on `BaseCv` is the server-generated sentence for
 * whichever of these applies — the client never re-implements that mapping.
 */
export type ExtractionFailureReason =
  'encrypted' | 'corrupt' | 'no_text_layer' | 'too_short' | 'too_many_pages' | 'extractor_error';

/** The three formats intake accepts, decided server-side from the file's bytes. */
export type CvContentType =
  | 'application/pdf'
  | 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  | 'text/plain';

/**
 * How a guest `BaseCv` came to be in this workspace (slice 2.2, additive on the wire): uploaded
 * here, or a **working copy** of one of the signed-in user's saved CVs (`POST
 * /api/base-cvs/copies`). Derived by the server from the row, never stored as a column — the client
 * only reads it, to decide whether the base-CV card shows the Working copy badge (AC-41).
 */
export type BaseCvOrigin = 'uploaded' | 'copied_from_saved';

/**
 * One `BaseCv`, as the client sees it.
 *
 * `extracted_text` is deliberately not a field here either — the API never returns it
 * (`schemas/intake.py`'s docstring): every byte of CV text that crosses the wire is a byte that
 * can land in a browser cache, a CDN, or an intermediary's access log, and nothing on the client
 * needs the text itself in this slice.
 */
export interface BaseCv {
  readonly id: string;
  readonly original_filename: string;
  readonly content_type: CvContentType;
  readonly size_bytes: number;
  readonly status: BaseCvStatus;
  readonly character_count: number | null;
  readonly failure_reason: ExtractionFailureReason | null;
  readonly failure_message: string | null;
  readonly uploaded_at: string;
  readonly expires_at: string;
  /**
   * Optional on purpose, although the 2.2 API always sends it. The deploy runs two versions briefly
   * (CLAUDE.md, expand → migrate → contract): for a moment the new bundle can talk to a 2.1 API
   * that has never heard of `origin`. **Absent means `'uploaded'`** — the only kind of guest CV a
   * 2.1 API could have made — so the badge is simply not shown, which is the true answer.
   */
  readonly origin?: BaseCvOrigin;
}

/** Every `BaseCv` a guest session owns. `items` is `[]` for a session with none. */
export interface BaseCvListResponse {
  readonly items: readonly BaseCv[];
}
