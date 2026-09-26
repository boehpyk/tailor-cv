import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { QueryClient } from '@tanstack/react-query';

import { AUTH_CHANNEL_NAME, __resetForTests, authStore } from './authStore';
import { connectCrossTabSignOut, currentUserQueryKey } from './hooks/authCache';

/**
 * T26 RED — AC-42, against feature-spec.md and technical-plan.md §7, never against
 * `authStore.ts`/`authCache.ts`'s T25 stubs (`connectChannel` listens to nothing;
 * `connectCrossTabSignOut` connects nothing; `broadcastSignOut` is a no-op). Every assertion below
 * fails because the dispatched `SIGNED_OUT` / removed queries never arrive — a `waitFor` timeout on
 * a real assertion, never an `ImportError`.
 *
 * **Real `BroadcastChannel`s, two of them** ("two stores, one channel", task-list T26): one stands
 * in for *this* tab (the store under test, connected via `authStore.connectChannel`), one for
 * *another* tab of the same origin, used only to post messages this tab should react to and to
 * listen for what this tab broadcasts. They share `AUTH_CHANNEL_NAME`, exactly as two real tabs
 * would, and a `BroadcastChannel` never delivers a message to the object that posted it — which is
 * what makes "posted on `channel`, observed on a *different* channel of the same name" the correct
 * shape for this test rather than a technicality.
 */

const AUTHENTICATED_RESPONSE = {
  access_token: 'token-1',
  token_type: 'Bearer' as const,
  expires_in: 900,
  user: { id: 'user-1', email: 'alex@example.com', created_at: '2026-09-25T10:00:00Z' },
};

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('authStore cross-tab sign-out (AC-42)', () => {
  it('broadcastSignOut posts exactly {type: "signed-out"} on the connected channel', async () => {
    const thisTabChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
    const otherTabChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
    const received: unknown[] = [];
    otherTabChannel.onmessage = (event: MessageEvent) => received.push(event.data);
    authStore.connectChannel(thisTabChannel, () => undefined);

    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    authStore.signOut('expired'); // local sign-out, e.g. what useLogout would have already done
    authStore.broadcastSignOut();

    await vi.waitFor(() => {
      expect(received).toEqual([{ type: 'signed-out' }]);
    });
    thisTabChannel.close();
    otherTabChannel.close();
  });

  it('setAuthenticated (signing in) is never broadcast', async () => {
    const thisTabChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
    const otherTabChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
    const received: unknown[] = [];
    otherTabChannel.onmessage = (event: MessageEvent) => received.push(event.data);
    authStore.connectChannel(thisTabChannel, () => undefined);

    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    // No broadcastSignOut() call — signing in never posts on its own.

    // No message is ever going to arrive, so this waits out a short, bounded window rather than
    // for something that (correctly) never happens.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(received).toEqual([]);
    thisTabChannel.close();
    otherTabChannel.close();
  });

  it(
    "receiving another tab's signed-out message dispatches SIGNED_OUT reason " +
      "'signed_out_elsewhere' and calls onSignedOutElsewhere — without calling /refresh or /logout",
    async () => {
      const fetchMock = vi.fn(() =>
        Promise.reject(new Error('no network call is expected on receipt of a cross-tab sign-out')),
      );
      vi.stubGlobal('fetch', fetchMock);

      const thisTabChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
      const otherTabChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
      let calledBack = 0;
      authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
      authStore.connectChannel(thisTabChannel, () => {
        calledBack += 1;
      });

      otherTabChannel.postMessage({ type: 'signed-out' });

      await vi.waitFor(() => {
        expect(authStore.getSnapshot()).toEqual({
          status: 'anonymous',
          reason: 'signed_out_elsewhere',
        });
      });
      expect(calledBack).toBe(1);
      expect(fetchMock).not.toHaveBeenCalled();
      thisTabChannel.close();
      otherTabChannel.close();
    },
  );

  it('a message of any other shape is ignored', async () => {
    const thisTabChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
    const otherTabChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
    let calledBack = 0;
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    authStore.connectChannel(thisTabChannel, () => {
      calledBack += 1;
    });

    otherTabChannel.postMessage({ type: 'something-else' });
    otherTabChannel.postMessage('signed-out');
    otherTabChannel.postMessage(null);

    // Bounded wait for "nothing happened" — there is no positive event to await here.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(authStore.getSnapshot()).toEqual({ status: 'authenticated' });
    expect(calledBack).toBe(0);
    thisTabChannel.close();
    otherTabChannel.close();
  });

  it('the disconnect function returned by connectChannel stops future messages from being honoured', async () => {
    const thisTabChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
    const otherTabChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
    authStore.setAuthenticated(AUTHENTICATED_RESPONSE);
    const disconnect = authStore.connectChannel(thisTabChannel, () => undefined);
    disconnect();

    otherTabChannel.postMessage({ type: 'signed-out' });

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(authStore.getSnapshot()).toEqual({ status: 'authenticated' });
    thisTabChannel.close();
    otherTabChannel.close();
  });
});

describe('authCache.connectCrossTabSignOut (AC-42, AC-33, AC-38 S-56)', () => {
  function makeQueryClient(): QueryClient {
    return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  }

  it(
    "another tab's sign-out removes every ['auth', …] query from this tab's cache and leaves a " +
      'guest-workspace query untouched, with no network call',
    async () => {
      const fetchMock = vi.fn(() =>
        Promise.reject(new Error('no network call is expected on receipt of a cross-tab sign-out')),
      );
      vi.stubGlobal('fetch', fetchMock);

      const thisTabChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
      const otherTabChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
      const queryClient = makeQueryClient();
      queryClient.setQueryData(currentUserQueryKey, {
        id: 'user-1',
        email: 'alex@example.com',
        created_at: '2026-09-25T10:00:00Z',
      });
      queryClient.setQueryData(['auth', 'savedBaseCvs', 'user-1'], { items: [{ id: 'cv-a' }] });
      queryClient.setQueryData(['base-cvs'], [{ id: 'guest-cv-1' }]);
      authStore.setAuthenticated(AUTHENTICATED_RESPONSE);

      connectCrossTabSignOut(queryClient, thisTabChannel);
      otherTabChannel.postMessage({ type: 'signed-out' });

      await vi.waitFor(() => {
        expect(queryClient.getQueryData(currentUserQueryKey)).toBeUndefined();
      });
      expect(queryClient.getQueryData(['auth', 'savedBaseCvs', 'user-1'])).toBeUndefined();
      expect(queryClient.getQueryData(['base-cvs'])).toEqual([{ id: 'guest-cv-1' }]);
      expect(fetchMock).not.toHaveBeenCalled();
      thisTabChannel.close();
      otherTabChannel.close();
    },
  );

  it('returns a disconnect that stops the cache from reacting to later messages', async () => {
    const thisTabChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
    const otherTabChannel = new BroadcastChannel(AUTH_CHANNEL_NAME);
    const queryClient = makeQueryClient();
    queryClient.setQueryData(currentUserQueryKey, {
      id: 'user-1',
      email: 'alex@example.com',
      created_at: '2026-09-25T10:00:00Z',
    });

    const disconnect = connectCrossTabSignOut(queryClient, thisTabChannel);
    disconnect();
    otherTabChannel.postMessage({ type: 'signed-out' });

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(queryClient.getQueryData(currentUserQueryKey)).toEqual({
      id: 'user-1',
      email: 'alex@example.com',
      created_at: '2026-09-25T10:00:00Z',
    });
    thisTabChannel.close();
    otherTabChannel.close();
  });
});
