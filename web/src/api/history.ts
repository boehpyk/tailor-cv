import { request } from './client';

import type { HistoryPage } from '@/features/history/types';

/**
 * A signed-in user's history — the collection `/api/me/tailoring-runs` read as pages, and the
 * deletion of one entry (slice 2.3, technical plan §4). Transport only.
 *
 * **Every call here is `auth: 'required'`** (AC-48): account data answers to the bearer and to
 * nothing else — the server ignores `tc_guest` on `/api/me/` — and the client sends the token
 * because the route needs it, never because one happens to be held.
 *
 * **Why the history collection is `/api/me/tailoring-runs`** and not `/api/me/history`: the resource
 * *is* the user's tailoring runs; "history" is the page that lists them. One collection, one noun.
 * The `POST` on the same path, the run itself and its documents live in `api/tailoringRuns.ts`,
 * which serves both scopes.
 *
 * Every refusal is an `ApiError`; callers branch on its `code`, never its `message`.
 */

/** The account's run collection. */
const HISTORY_PATH = '/api/me/tailoring-runs';

/**
 * How many entries one page asks for. The server's own default is the same number
 * (`HistoryPageSize.DEFAULT`); it is sent explicitly so a page's size is stated where it is read.
 */
export const HISTORY_PAGE_SIZE = 20;

/**
 * One page of history, newest first. `cursor` is `null` for the first page, else the previous
 * page's `next_cursor`, **passed through verbatim** — it is opaque, and the client never builds,
 * parses or compares one.
 *
 * Refusals: 401 `invalid_access_token` / `not_signed_in`; 422 `validation_error` / `invalid_cursor`;
 * 503 `service_unavailable`.
 */
export function fetchHistoryPage(
  cursor: string | null,
  signal?: AbortSignal,
  limit = HISTORY_PAGE_SIZE,
): Promise<HistoryPage> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor !== null) {
    query.set('cursor', cursor);
  }
  return request<HistoryPage>(`${HISTORY_PATH}?${query.toString()}`, {
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

/**
 * Delete one history entry — the run, its export jobs and their files, and its posting if no other
 * entry uses it: **204**. Irreversible (ADR-0006 §2's order: rows committed, then files).
 *
 * A 404 `tailoring_run_not_found` means it is already gone (a second tab, a double submit): thrown
 * like any refusal here and read as "already gone" one layer up — what a status *means* is not
 * transport's call. A 409 `tailoring_run_in_progress` (carries `status`) means the run is still
 * `queued`/`running` and was **not** deleted; there is no cancellation in this product. A 503
 * `service_unavailable` means it was not deleted either.
 */
export async function deleteHistoryEntry(id: string, signal?: AbortSignal): Promise<void> {
  await request<null>(`${HISTORY_PATH}/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}
