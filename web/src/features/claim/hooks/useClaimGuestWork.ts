import { useMutation, useQueryClient } from '@tanstack/react-query';

import { claimGuestWork } from '@/api/guestWork';
import { savedBaseCvsQueryKey } from '@/features/savedCvs/hooks/savedCvsKeys';
import { GUEST_QUERY_ROOTS, accountKeyRoot } from '@/features/scope/scopeMap';

import type { GuestWorkClaimResult } from '../types';
import type { UseMutationResult } from '@tanstack/react-query';

/**
 * The claim's mutation key: under the account's root (`['auth', 'account', userId]`), so 2.1's
 * `removeQueries(['auth'])` family — logout, account deletion, a cross-tab sign-out — owns it too,
 * and two users in one tab never share it (AC-42). The `isMutating({ mutationKey })` guard against a
 * same-tick double click reads it (AC-38, 2.1's trap).
 */
export function claimGuestWorkMutationKey(userId: string): readonly unknown[] {
  return [...accountKeyRoot(userId), 'claimGuestWork'];
}

/**
 * **Keep them in my account**: `POST /api/me/guest-work/claim` for the signed-in `userId`
 * (technical plan §0.11, §7).
 *
 * On success — in the hook's own `onSuccess`, so it runs even if the caller unmounts, and **before**
 * any navigation the caller does (2.2's rule) — every `GUEST_QUERY_ROOTS` entry is removed from the
 * cache (the session they describe no longer exists), and the account's data is marked stale: the
 * account root **and** the saved-CV list, which lives at `['auth', 'savedBaseCvs', userId]`, outside
 * that root (2.2 keyed it before 2.3 introduced the root). Invalidating only the root would leave the
 * claimed CVs missing from the picker and `/account` until the next mount.
 *
 * The invalidations are not awaited: the claim is done when the server says so, and the re-reads
 * belong to whichever screens are showing the data. A failure touches no cache — nothing moved.
 *
 * Variables are `void`: the claim takes no input — what moves is everything the cookie's session
 * owns, decided on the server.
 */
export function useClaimGuestWork(
  userId: string,
): UseMutationResult<GuestWorkClaimResult, Error, void> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: claimGuestWorkMutationKey(userId),
    mutationFn: () => claimGuestWork(),
    onSuccess: () => {
      for (const root of GUEST_QUERY_ROOTS) {
        queryClient.removeQueries({ queryKey: root });
      }
      void queryClient.invalidateQueries({ queryKey: accountKeyRoot(userId) });
      void queryClient.invalidateQueries({ queryKey: savedBaseCvsQueryKey(userId) });
    },
  });
}
