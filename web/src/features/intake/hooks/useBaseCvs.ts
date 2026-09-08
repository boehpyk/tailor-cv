import { useQuery } from '@tanstack/react-query';

import { fetchBaseCvs } from '@/api/baseCvs';
import { ApiError } from '@/api/client';

import type { BaseCvListResponse } from '../types';

export const baseCvsQueryKey = ['intake', 'baseCvs'] as const;

/**
 * The guest session's base CVs.
 *
 * **The 401 decision (F-19):** `GET /api/base-cvs` answers 401 `guest_session_expired` for a
 * missing, unknown or expired `tc_guest` cookie, with no body. The feature spec is explicit about
 * what that should look like to the user: "The UI clears local state and shows the fresh
 * dropzone" — in other words, a session with no cookie left and a session that legitimately owns
 * zero CVs are the **same screen**. So this hook folds `guest_session_expired` into the ordinary
 * empty result (`{ items: [] }`) inside `queryFn`, rather than letting it surface as `isError`.
 *
 * That is a deliberate choice about where the branch lives, not the only place it could: the
 * alternative is exposing `isError`/`error.code` and having `BaseCvUploadPanel` (T33) treat that
 * one code as its empty state. Doing it here instead means every consumer of this hook — today's
 * panel and anything later — gets "no session" and "no CVs yet" collapsed for free, with exactly
 * one place that knows a 401 on this endpoint is not a failure. Any other `ApiError` (a genuine
 * 5xx, a network failure, or a future different 4xx) is rethrown untouched and lands in the
 * query's normal `isError` branch — those are real "this failed, try again" states, and folding
 * them in here too would erase the distinction F-19 exists to draw.
 *
 * A cleared or expired session cannot be told apart from "zero CVs" from this response alone, and
 * that is intentional (ADR-0010): the alternative — a body on a 401 for an unauthenticated request
 * — would let a client fish for whether a `tc_guest` cookie was ever valid.
 */
export function useBaseCvs(): ReturnType<typeof useQuery<BaseCvListResponse>> {
  return useQuery({
    queryKey: baseCvsQueryKey,
    queryFn: async ({ signal }) => {
      try {
        return await fetchBaseCvs(signal);
      } catch (error) {
        if (
          error instanceof ApiError &&
          error.status === 401 &&
          error.code === 'guest_session_expired'
        ) {
          return { items: [] };
        }
        throw error;
      }
    },
  });
}
