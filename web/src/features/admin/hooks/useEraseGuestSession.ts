import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router';

import { eraseGuestSession } from '@/api/admin';
import { ApiError } from '@/api/client';

import { guestDeletedNotice } from '../adminCopy';
import { contentNoticeState } from '../contentNotice';

import { adminGuestKey } from './useAdminUser';
import { guestListKey } from './useGuestSessions';

import type { GuestSessionErasure } from '@/api/admin';
import type { UseMutationResult } from '@tanstack/react-query';

export type GuestSessionErasureOutcome =
  | { readonly outcome: 'erased'; readonly report: GuestSessionErasure }
  /** 404 `guest_session_not_found`: it expired, was claimed or was deleted first. */
  | { readonly outcome: 'already_gone' };

/** A caller guards a same-tick double click with `isMutating({ mutationKey })` (2.1). */
export const eraseGuestSessionMutationKey = [
  'auth',
  'account',
  'admin',
  'eraseGuestSession',
] as const;

/** Where the list lives, relative to nothing: `/admin/guests`. */
export const GUEST_LIST_PATH = '/admin/guests';

async function erase(sessionId: string): Promise<GuestSessionErasureOutcome> {
  try {
    return { outcome: 'erased', report: await eraseGuestSession(sessionId) };
  } catch (error) {
    if (
      error instanceof ApiError &&
      error.status === 404 &&
      error.code === 'guest_session_not_found'
    ) {
      return { outcome: 'already_gone' };
    }
    throw error;
  }
}

/**
 * Delete one guest session ahead of the purge (AC-34). **Not optimistic**, never re-sent
 * (ADR-0031); mutations do not retry, so a 4xx is shown, not repeated.
 *
 * - **Erased:** navigate to the list with the notice **first** (so no page under the session's key
 *   is mounted, and because TanStack skips a mutate-level callback on an unmounted observer, 2.2),
 *   then remove the session's key **by prefix** — its content bodies sit under it, and
 *   `exact: true` would keep them (4.3's `301c75a`) — then invalidate the list.
 * - **Already gone:** stay; invalidate the session's key (its refetch answers the gone sentence)
 *   and the list.
 */
export function useEraseGuestSession(
  actorId: string,
  sessionId: string,
): UseMutationResult<GuestSessionErasureOutcome, Error, void> {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  return useMutation({
    mutationKey: eraseGuestSessionMutationKey,
    mutationFn: () => erase(sessionId),
    onSuccess: async (result) => {
      const sessionKey = adminGuestKey(actorId, sessionId);
      if (result.outcome === 'erased') {
        await navigate(GUEST_LIST_PATH, {
          state: contentNoticeState({
            text: guestDeletedNotice(result.report.files_unlinked),
            unlinkFailures: result.report.unlink_failures,
          }),
        });
        queryClient.removeQueries({ queryKey: sessionKey });
      } else {
        await queryClient.invalidateQueries({ queryKey: sessionKey });
      }
      await queryClient.invalidateQueries({ queryKey: guestListKey(actorId) });
    },
  });
}
