import { useMutation, useQueryClient } from '@tanstack/react-query';

import { disableUser, enableUser, eraseUser } from '@/api/admin';
import { authStore } from '@/features/auth/authStore';
import { authQueryKeyPrefix } from '@/features/auth/hooks/authCache';

import { adminUserKey } from './useAdminUser';
import { userDirectoryRootKey } from './useUserDirectory';

import type { AccountErasureReport, AccountStatusChange, AdminUserDetail } from '@/api/admin';
import type { SignOutReason } from '@/features/auth/authMachine';
import type { UseMutationResult } from '@tanstack/react-query';

export type AccountAction = 'disable' | 'enable' | 'erase';

export type AccountActionResult =
  | { readonly action: 'disable' | 'enable'; readonly change: AccountStatusChange }
  | { readonly action: 'erase'; readonly report: AccountErasureReport };

/** One key for every action: a caller guards a same-tick double click with `isMutating` (2.1). */
export const accountActionMutationKey = ['auth', 'account', 'admin', 'accountAction'] as const;

async function perform(action: AccountAction, userId: string): Promise<AccountActionResult> {
  switch (action) {
    case 'disable':
      return { action, change: await disableUser(userId) };
    case 'enable':
      return { action, change: await enableUser(userId) };
    case 'erase':
      return { action, report: await eraseUser(userId) };
  }
}

export interface UseAccountActionOptions {
  /**
   * Runs, awaited, after an erase of **another** account and before the cache is touched — where
   * the page navigates to `/admin` with the notice. From the mutation's own `onSuccess`, because
   * TanStack skips a mutate-level callback on an unmounted observer (2.2).
   */
  readonly onErased?: (report: AccountErasureReport) => Promise<void> | void;
}

/**
 * Disable, enable or erase `userId` as `actorId` (AC-34, AC-35, AC-38). **Not optimistic**: the
 * page shows what the server did. A refusal changes nothing locally and is never re-sent (writes
 * hold, ADR-0031).
 *
 * On success:
 * - **the actor's own account** (OQ-18): the tab signs out — `account_disabled` or
 *   `account_deleted` — store first, then the `['auth', …]` cache, then the other tabs
 *   (`useDeleteAccount`'s order); `RequireAuth` takes it to `/` with the notice;
 * - **disable / enable** of another: the detail takes the answer's `disabled_at`, the directory
 *   is invalidated;
 * - **erase** of another: `onErased` (the navigation), then the detail is removed and the
 *   directory invalidated.
 */
export function useAccountAction(
  actorId: string,
  userId: string,
  { onErased }: UseAccountActionOptions = {},
): UseMutationResult<AccountActionResult, Error, AccountAction> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: accountActionMutationKey,
    mutationFn: (action: AccountAction) => perform(action, userId),
    onSuccess: async (result) => {
      if (actorId === userId && result.action !== 'enable') {
        const reason: SignOutReason =
          result.action === 'erase' ? 'account_deleted' : 'account_disabled';
        authStore.signOut(reason);
        queryClient.removeQueries({ queryKey: authQueryKeyPrefix });
        authStore.broadcastSignOut();
        return;
      }
      const detailKey = adminUserKey(actorId, userId);
      if (result.action === 'erase') {
        await onErased?.(result.report);
        queryClient.removeQueries({ queryKey: detailKey, exact: true });
      } else {
        queryClient.setQueryData<AdminUserDetail>(detailKey, (detail) =>
          detail === undefined ? detail : { ...detail, disabled_at: result.change.disabled_at },
        );
      }
      await queryClient.invalidateQueries({ queryKey: userDirectoryRootKey(actorId) });
    },
  });
}
