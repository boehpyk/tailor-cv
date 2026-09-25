import { useQueryClient } from '@tanstack/react-query';
import { useCallback, useSyncExternalStore } from 'react';

import { authStore } from '../authStore';

import { seedFromRefresh } from './authCache';
import { useCurrentUser } from './useCurrentUser';

import type { SignOutReason } from '../authMachine';
import type { User } from '../types';

/**
 * What a component may know about auth — one discriminated union, so "authenticated" and "still
 * checking" cannot both be true in one render, and the Retry action exists only on the state that
 * offers it.
 *
 * `user` is `undefined` only in the instant before `['auth', 'me']` is seeded (or, after a reload
 * past its `staleTime`, while `/me` is in flight). No variant carries the access token: the store's
 * snapshot does not have it to give (AC-35).
 */
export type AuthView =
  | { readonly status: 'booting' }
  | { readonly status: 'anonymous'; readonly reason: SignOutReason | null }
  | { readonly status: 'unavailable'; readonly retry: () => void }
  | { readonly status: 'authenticated'; readonly user: User | undefined };

/**
 * **The** auth hook (AC-35): the store's state through `useSyncExternalStore`, the user through
 * TanStack Query. Components read auth from here and nowhere else.
 *
 * `useSyncExternalStore` rather than `useState` + `useEffect(subscribe)`: the store changes outside
 * React (a refresh finishing inside `api/client.ts`), and this hook is what React ships to read such
 * a value without tearing — every component in one render sees the same state.
 */
export function useAuth(): AuthView {
  const snapshot = useSyncExternalStore(authStore.subscribe, authStore.getSnapshot);
  const currentUser = useCurrentUser();
  const queryClient = useQueryClient();

  // The Retry on "couldn't check whether you're logged in" re-asks the boot question, so its
  // answer seeds the cache exactly as the boot's does. `refresh()` never rejects.
  const retry = useCallback(() => {
    void authStore.retry().then((result) => {
      seedFromRefresh(queryClient, result);
    });
  }, [queryClient]);

  switch (snapshot.status) {
    case 'booting':
      return { status: 'booting' };
    case 'anonymous':
      return { status: 'anonymous', reason: snapshot.reason };
    case 'unavailable':
      return { status: 'unavailable', retry };
    case 'authenticated':
      return { status: 'authenticated', user: currentUser.data };
  }
}
