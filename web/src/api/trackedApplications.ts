import { request } from './client';

import type {
  Board,
  Stage,
  TrackApplicationInput,
  TrackedApplication,
} from '@/features/tracking/types';

/**
 * The application board (slice 3.1, technical plan §4) — `GET /api/me/board` and the four writes on
 * `/api/me/tracked-applications`. Transport only.
 *
 * **Every call here is `auth: 'required'`** (AC-22): a card is account data, answering to the bearer
 * and to nothing else; the server never reads `tc_guest` on these routes. `api/client.ts` attaches
 * the token, refreshes first when it is near expiry, and answers one 401 `invalid_access_token` with
 * one refresh and one retry.
 *
 * **The user id is in no URL.** The bearer says whose board it is; the id lives only in the query
 * keys (`features/tracking/hooks/trackingKeys.ts`), where it keeps two users in one tab apart.
 *
 * Every refusal is an `ApiError`; callers branch on its `code` (narrowed with `isTrackingErrorCode`),
 * never on its `message`. What a code *means* to the user — a 409 `application_already_tracked`
 * being a success, a 404 on a move being "gone" — is decided one layer up, not here.
 *
 * Common to every call: 401 `invalid_access_token` / `not_signed_in`; 503 `service_unavailable`.
 */

const BOARD_PATH = '/api/me/board';
const TRACKED_APPLICATIONS_PATH = '/api/me/tracked-applications';

/** The path of one card. The id came from the server, but it is still encoded, not trusted. */
function trackedApplicationPath(id: string): string {
  return `${TRACKED_APPLICATIONS_PATH}/${encodeURIComponent(id)}`;
}

/**
 * The whole board, `stage_changed_at` newest first — `{ items: [] }` when empty, never a 404.
 * Unlimited (no rate limit on the read). Refusals: the common three only.
 */
export function fetchBoard(signal?: AbortSignal): Promise<Board> {
  return request<Board>(BOARD_PATH, {
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

/**
 * Put a succeeded run on the board: **201** `TrackedApplication`.
 *
 * Refusals: 404 `tailoring_run_not_found` (not this account's, or gone — byte-identical); 409
 * `tailoring_run_not_trackable` (not `succeeded`); 409 `application_already_tracked` (details carry
 * `tracked_application_id`); 409 `too_many_tracked_applications`; 422 `validation_error` (the title
 * is never echoed); 429 `rate_limited` (`retryAfterSeconds`).
 */
export function trackApplication(
  input: TrackApplicationInput,
  signal?: AbortSignal,
): Promise<TrackedApplication> {
  return request<TrackedApplication>(TRACKED_APPLICATIONS_PATH, {
    method: 'POST',
    body: input,
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

/**
 * Move a card to `stage`, against the `version` the client last saw: **200** with the server's card
 * (its new `version`; unchanged when the stage was already `stage`, T-24).
 *
 * Refusals: 404 `tracked_application_not_found`; 409 `tracked_application_version_conflict` (details
 * carry `current_version: number | null`); 422 `validation_error`; 429 `rate_limited`.
 */
export function moveTrackedApplication(
  id: string,
  stage: Stage,
  version: number,
  signal?: AbortSignal,
): Promise<TrackedApplication> {
  return request<TrackedApplication>(`${trackedApplicationPath(id)}/stage`, {
    method: 'PUT',
    body: { stage, version },
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

/**
 * Set a card's title, or clear it with `null`: **200** with the server's card. The title is sent as
 * typed — trimming, the 120-character limit and the ban on control characters are the server's
 * (`ApplicationTitle`), refused as 422 `validation_error`, never re-implemented here.
 *
 * Refusals: as `moveTrackedApplication`.
 */
export function retitleTrackedApplication(
  id: string,
  title: string | null,
  version: number,
  signal?: AbortSignal,
): Promise<TrackedApplication> {
  return request<TrackedApplication>(`${trackedApplicationPath(id)}/title`, {
    method: 'PUT',
    body: { title, version },
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}

/**
 * Take a card off the board: **204**. The run, its documents and exports are untouched — it is still
 * in the history. A 404 `tracked_application_not_found` means it is already gone, thrown like any
 * refusal and read as "gone" one layer up. Also 429 `rate_limited`.
 */
export async function untrackApplication(id: string, signal?: AbortSignal): Promise<void> {
  await request<null>(trackedApplicationPath(id), {
    method: 'DELETE',
    auth: 'required',
    ...(signal ? { signal } : {}),
  });
}
