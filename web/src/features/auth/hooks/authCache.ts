import { AUTH_CHANNEL_NAME, authStore } from '../authStore';

import type { AuthChannel, RefreshResult } from '../authStore';
import type { AuthenticatedResponse } from '../types';
import type { QueryClient } from '@tanstack/react-query';

/**
 * The bridge between the two homes of auth data (technical plan §0.6): the **token** in
 * `authStore`, the **user** in TanStack Query under `['auth', 'me']`. One datum, one home — and
 * these few functions are the only places that write both, so the order of the two writes is
 * decided once.
 *
 * Plain functions, not hooks: `main.tsx` calls `seedFromRefresh` with its module-scope
 * `queryClient` after `authStore.bootstrap()` (T43), and the hooks call these with
 * `useQueryClient()`'s. Nothing here ever puts the access token into the cache — only `user`.
 */

/** Every auth query lives under this prefix, so logout can remove exactly them (AC-44). */
export const authQueryKeyPrefix = ['auth'] as const;

/** The current user's profile — the one piece of auth state that is server state. */
export const currentUserQueryKey = ['auth', 'me'] as const;

/**
 * A login succeeded (since slice 2.5 the only thing that signs in by credentials: registering
 * answers 202 and signs nobody in). **Seed the cache first, then flip the store.** The flip is what
 * enables `useCurrentUser`; if it came first, the render it triggers could find the query enabled
 * and empty and send a `GET /api/auth/me` the response in hand already answered.
 */
export function acceptAuthenticated(
  queryClient: QueryClient,
  response: AuthenticatedResponse,
): void {
  queryClient.setQueryData(currentUserQueryKey, response.user);
  authStore.setAuthenticated(response);
}

/**
 * Act on how a boot (or a Retry's) refresh ended. By the time this runs the store has already
 * dispatched; this only keeps the cache in step with it.
 *
 * - `authenticated` → seed `['auth', 'me']` with the profile the token response carried.
 * - `anonymous` → drop every `['auth', …]` query, so a later login never flashes the previous
 *   user's profile.
 * - `unavailable` → nothing: we do not know who this is, and the store says so.
 * - `superseded` → nothing: the reducer ignored this answer because something newer decided the
 *   state, and that newer thing (a login's `acceptAuthenticated`, a logout) wrote the cache itself.
 *   Seeding here could write a *different* user over the one now logged in.
 */
export function seedFromRefresh(queryClient: QueryClient, result: RefreshResult): void {
  switch (result.kind) {
    case 'authenticated':
      queryClient.setQueryData(currentUserQueryKey, result.user);
      return;
    case 'anonymous':
      queryClient.removeQueries({ queryKey: authQueryKeyPrefix });
      return;
    case 'unavailable':
    case 'superseded':
      return;
  }
}

/**
 * Hear other tabs' sign-outs (slice 2.2, AC-42): connect the store to `channel` — by default a new
 * `BroadcastChannel(AUTH_CHANNEL_NAME)` — so that another tab's logout or account deletion sets this
 * tab `anonymous` **and** removes every `['auth', …]` query from `queryClient`, with no network
 * call. The guest workspace's queries stay: that work belongs to this browser, not to the user.
 *
 * Called once, at module scope in `main.tsx`, with the app's one `queryClient`. Returns a
 * disconnect. In a browser without `BroadcastChannel` there is nothing to hear, and this returns a
 * no-op rather than throwing — a tab that cannot hear other tabs still converges on its next request.
 *
 * **Store first, cache second** (`authStore.connectChannel`): by the time the queries are removed
 * the store is `anonymous`, so every `['auth', …]` query is already disabled and none refetches.
 */
export function connectCrossTabSignOut(
  queryClient: QueryClient,
  channel?: AuthChannel,
): () => void {
  let target = channel;
  // A channel this function opened is this function's to close; one handed in belongs to the caller.
  let owned: BroadcastChannel | null = null;
  if (target === undefined) {
    if (typeof BroadcastChannel === 'undefined') {
      return () => undefined;
    }
    owned = new BroadcastChannel(AUTH_CHANNEL_NAME);
    target = owned;
  }
  const disconnect = authStore.connectChannel(target, () => {
    queryClient.removeQueries({ queryKey: authQueryKeyPrefix });
  });
  return () => {
    disconnect();
    owned?.close();
  };
}
