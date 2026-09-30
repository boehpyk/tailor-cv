import { request } from './client';
import { authOptionFor } from './target';

import type {
  NewTailoringRun,
  ReviseDocumentBody,
  TailoredDocumentKind,
  TailoringRun,
  TailoringRunListResponse,
} from '@/features/tailoring/types';
import type { ApiTarget } from './target';

/**
 * The tailoring-run endpoints. All four are plain JSON; the only change `client.ts` ever needed
 * for them was `PUT` in its method union.
 *
 * **Unlike the base-CV and job-posting POSTs, all four of these answer 401
 * `guest_session_expired` for a missing, unknown or expired cookie — the writes included.** The
 * API mints no session here (AC-16, AC-15): a run names a base CV and a posting the session must
 * already own, so a request without a valid session has nothing to tailor and nothing to edit.
 * Each function throws that 401 as an `ApiError` like any other failure; what it *means* for the
 * UI is decided one layer up.
 *
 * **Slice 2.3: three of the four serve both scopes.** The run, its creation and its revision take an
 * `ApiTarget` — the guest routes with the cookie, or their `/api/me/` twins with the bearer (plan
 * §0.2, §0.9) — because the bodies and the answers are the same and two copies would drift. For an
 * account the 401s are `invalid_access_token` / `not_signed_in`, and the client's refresh-and-retry
 * handles the first. The *list* stays guest-only: the account's runs are its history, read as pages
 * (`api/history.ts`), a different shape on purpose.
 */

/**
 * Every run the caller's guest session owns, newest first, as summaries **without** the two
 * documents. `items` is `[]` for a session with none — never a 404. The 401 fold lives in
 * `useTailoringRuns`, not here.
 */
export function fetchTailoringRuns(signal?: AbortSignal): Promise<TailoringRunListResponse> {
  return request<TailoringRunListResponse>('/api/tailoring-runs', {
    ...(signal ? { signal } : {}),
  });
}

/**
 * One run in full, including both documents once it has succeeded. **This is the endpoint the
 * poller reads**, and a run that failed is a 200 with `status: 'failed'`, not a thrown error — the
 * only 4xx is 404 `tailoring_run_not_found` (identical for "does not exist" and "not yours") and the
 * session's 401.
 */
export function fetchTailoringRun(
  target: ApiTarget,
  id: string,
  signal?: AbortSignal,
): Promise<TailoringRun> {
  return request<TailoringRun>(`${target.tailoringRunsPath}/${encodeURIComponent(id)}`, {
    ...authOptionFor(target),
    ...(signal ? { signal } : {}),
  });
}

/**
 * Request one tailoring run. The API answers **202** with the run already `queued` and every
 * document field `null`; the caller polls `fetchTailoringRun(result.id)` until it is terminal.
 * (`client.ts` treats any 2xx as success, so a 202 needs no special handling.)
 *
 * Rejections arrive as `ApiError`s keyed by `code`: 401 `guest_session_expired`; 404
 * `base_cv_not_found` / `job_posting_not_found`; 409 `base_cv_not_extracted`,
 * `tailoring_already_running` or `too_many_tailoring_runs`; 413 `request_too_large`; 422
 * `validation_error`; 429 `rate_limited`; 503 `rate_limit_unavailable`, `queue_unavailable` or
 * `service_unavailable`.
 */
export function createTailoringRun(
  target: ApiTarget,
  input: NewTailoringRun,
  signal?: AbortSignal,
): Promise<TailoringRun> {
  return request<TailoringRun>(target.tailoringRunsPath, {
    method: 'POST',
    body: input,
    ...authOptionFor(target),
    ...(signal ? { signal } : {}),
  });
}

/**
 * Replace the current text of one of a run's documents, in full — `PUT`, because that is what it
 * is (ADR-0015 §1: no revision history to post into). `kind` is a path segment, and it is typed
 * so an unknown one cannot be written.
 *
 * **The response is the whole run, not an acknowledgement**, and that is the point: the caller
 * writes it straight into the query cache under `['tailoring', 'tailoringRun', runId]` — the same
 * shape the poller reads — so `version`, both character counts and `*_edited_at` are current the
 * moment the save lands, with no second read and no client-side arithmetic on the version number.
 *
 * Rejections arrive as `ApiError`s keyed by `code`, and two of them carry a fact in `details` the
 * caller acts on (narrow it with `typeof` at the point of use, as `activeTailoringRunId` does):
 * 401 `guest_session_expired`; 404 `tailoring_run_not_found`; 409 `document_version_conflict`
 * (`current_version: number | null` — `DocumentVersionConflictDetails`) or
 * `tailoring_run_not_editable` (`status` — `TailoringRunNotEditableDetails`); 413
 * `request_too_large`; 422 `validation_error` or `document_invalid` (`problem` —
 * `DocumentInvalidDetails`); 429 `rate_limited`; 503 `service_unavailable`.
 *
 * `content` is a person's rewritten employment history. It goes in the body and nowhere else — not
 * in the URL, not in a log line, not in an error thrown from here.
 */
export function reviseTailoredDocument(
  target: ApiTarget,
  runId: string,
  kind: TailoredDocumentKind,
  body: ReviseDocumentBody,
  signal?: AbortSignal,
): Promise<TailoringRun> {
  return request<TailoringRun>(
    `${target.tailoringRunsPath}/${encodeURIComponent(runId)}/documents/${kind}`,
    {
      method: 'PUT',
      body,
      ...authOptionFor(target),
      ...(signal ? { signal } : {}),
    },
  );
}
