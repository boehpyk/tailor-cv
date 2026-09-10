/**
 * Mirrors `JobPostingResponse` / `JobPostingSummary` / `JobPostingListResponse` and the request
 * union in the API's `infrastructure/api/schemas/posting.py`, field for field.
 *
 * Hand-written for now, and that is a known seam (`features/intake/types.ts` carries the same
 * note): two declarations of one contract drift. When the API surface grows past a couple of
 * endpoints, generate these from the OpenAPI schema FastAPI already publishes at `/openapi.json`
 * rather than maintaining them by hand.
 */

/**
 * How the posting arrived. Mirrors the domain's `PostingSource` — a closed set, so a union of
 * literals rather than `string`: an unhandled member is a compile error here, not a silent
 * fallthrough at runtime.
 */
export type PostingSource = 'pasted' | 'fetched';

/**
 * The request body. A tagged union on `source`, mirroring the Pydantic discriminated union on the
 * wire and the `match` over two frozen command dataclasses in the use case — the same shape at
 * every boundary, on purpose (ADR-0013).
 *
 * Written as a union rather than `{ source: PostingSource; text?: string; url?: string }` for the
 * reason that shape fails: the optional-field version makes `{ source: 'pasted', url: '…' }`
 * type-check, and the API answers 422 for it. Here it does not compile, which is the earlier and
 * cheaper place to find out.
 */
export type NewJobPosting =
  | { readonly source: 'pasted'; readonly text: string }
  | { readonly source: 'fetched'; readonly url: string };

/**
 * One job posting in full, as `GET /api/job-postings/{id}` returns it.
 *
 * **`text` is present here, unlike `BaseCv`**, where the API deliberately returns only a character
 * count. A CV is dense PII the browser did not need; a job posting is content the user is about to
 * read and, in slice 1.4, edit. Both `GET`s answer `Cache-Control: no-store` so it does not land
 * in a shared cache.
 *
 * There is no `status` field, and the absence is the contract: a posting that exists is complete
 * (ADR-0013). A fetch that failed produced no posting at all and was an HTTP error.
 */
export interface JobPosting {
  readonly id: string;
  readonly source: PostingSource;
  readonly source_url: string | null;
  readonly title: string | null;
  readonly character_count: number;
  readonly text: string;
  readonly created_at: string;
  readonly expires_at: string;
}

/**
 * One job posting as it appears in a list: everything except the full text, plus a 280-character
 * `preview`. The list endpoint returns these rather than full postings because five postings at
 * 30,000 characters is 150 KB of user content in one response.
 */
export interface JobPostingSummary {
  readonly id: string;
  readonly source: PostingSource;
  readonly source_url: string | null;
  readonly title: string | null;
  readonly character_count: number;
  readonly preview: string;
  readonly created_at: string;
  readonly expires_at: string;
}

/** Every job posting a guest session owns. `items` is `[]` for a session with none. */
export interface JobPostingListResponse {
  readonly items: readonly JobPostingSummary[];
}

/**
 * The error codes this feature branches on, as a closed set.
 *
 * Split into two groups because **the UI treats them differently, and that difference is FR-2**:
 * a `FETCH_FAILURE_CODE` means the link could not be read and the answer is to paste the text
 * instead, while everything else means the request itself was refused. Only one of the two is
 * fixed by changing the input, and a user who cannot tell them apart will retry something that
 * will fail identically.
 *
 * Branch on `code`, never on `message` — `code` is the contract, `message` is prose that can be
 * reworded without notice (`api/client.ts`).
 */
export const FETCH_FAILURE_CODES = [
  'fetch_blocked',
  'source_unreachable',
  'source_timed_out',
  'source_rejected',
  'source_too_many_redirects',
  'source_response_too_large',
  'source_not_html',
  'source_no_readable_text',
  'source_text_too_long',
  'fetcher_error',
] as const;

export type FetchFailureCode = (typeof FETCH_FAILURE_CODES)[number];

/** Whether an API error code means "the link could not be read" rather than "the request was
 * refused". The type predicate is what lets a component narrow without a cast. */
export function isFetchFailureCode(code: string | null): code is FetchFailureCode {
  return code !== null && (FETCH_FAILURE_CODES as readonly string[]).includes(code);
}
