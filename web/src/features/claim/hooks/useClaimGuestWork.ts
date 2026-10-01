/* eslint-disable @typescript-eslint/no-unused-vars -- T29 SKELETON: the parameters are the signature qa's T30 tests compile against; T31 uses them and deletes this line. */
import { useMutation } from '@tanstack/react-query';

import { accountKeyRoot } from '@/features/scope/scopeMap';

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
 * cache (the session they describe no longer exists) and the account root is invalidated (the
 * account now holds the work). Navigating is the caller's job.
 *
 * Variables are `void`: the claim takes no input — what moves is everything the cookie's session
 * owns, decided on the server.
 *
 * SKELETON (T29): no mutation key, no request, no cache work — `mutate()` settles as an error;
 * T31 implements it.
 */
export function useClaimGuestWork(
  _userId: string,
): UseMutationResult<GuestWorkClaimResult, Error, void> {
  return useMutation({
    mutationFn: (): Promise<GuestWorkClaimResult> =>
      Promise.reject(new Error('useClaimGuestWork: not implemented (T29 skeleton)')),
  });
}
