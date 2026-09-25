import { QueryClient } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { jsonResponse } from '@/test/fixtures';

import { __resetForTests, authStore } from '../authStore';
import { acceptAuthenticated, currentUserQueryKey, seedFromRefresh } from './authCache';

import type { AuthenticatedResponse, User } from '../types';

/**
 * T44 (`qa`, test-after) — the store's own docstring calls out a case the RED-first tiers never
 * named: a boot refresh that answers **200 for a different user** than the one who has since logged
 * in. `RefreshResult`'s `superseded` variant and `seedFromRefresh`'s matching no-op both exist for
 * exactly this, past what AC-36's "the boot's own late 401 does not undo a meanwhile-login" case
 * covers — `authStore.test.ts`'s "the race rule" test only drives the 401 side.
 *
 * Two tests: one drives the real race end-to-end through `authStore` (the scenario the docstring
 * describes), one drives `seedFromRefresh` directly against a synthetic `superseded` result (the
 * contract in isolation, independent of how the store produces it).
 */

function authResponse(overrides: Partial<AuthenticatedResponse> = {}): AuthenticatedResponse {
  return {
    access_token: 'access-token-1',
    token_type: 'Bearer',
    expires_in: 900,
    user: { id: 'user-1', email: 'alex@example.com', created_at: '2026-09-23T10:00:00Z' },
    ...overrides,
  };
}

const USER_A: User = { id: 'user-a', email: 'a@example.com', created_at: '2026-09-23T10:00:00Z' };
const USER_B: User = { id: 'user-b', email: 'b@example.com', created_at: '2026-09-23T10:00:00Z' };

function makeQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
}

beforeEach(() => {
  vi.useRealTimers();
  __resetForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('the boot-vs-meanwhile-login race, carried through to the cache', () => {
  it(
    'boot refresh in flight, setAuthenticated for user B meanwhile, the boot then answers 200 ' +
      "for user A -> refresh() resolves superseded and seedFromRefresh does not overwrite B's profile",
    async () => {
      let resolveBootFetch: (response: Response) => void = () => {
        throw new Error('resolveBootFetch called before it was assigned');
      };
      const bootFetchPromise = new Promise<Response>((resolve) => {
        resolveBootFetch = resolve;
      });
      const fetchMock = vi.fn((input: string | URL, init?: RequestInit): Promise<Response> => {
        const url = typeof input === 'string' ? input : input.toString();
        const method = init?.method ?? 'GET';
        if (url === '/api/auth/refresh' && method === 'POST') {
          return bootFetchPromise;
        }
        return Promise.reject(new Error(`unhandled fetch: ${method} ${url}`));
      });
      vi.stubGlobal('fetch', fetchMock);

      const queryClient = makeQueryClient();
      const bootPromise = authStore.bootstrap();

      // The boot's own request is still unresolved. A real login for a DIFFERENT user (B) lands —
      // through the same function `LoginPage` uses, so the cache is seeded the way it would be in
      // the app, not by poking `setQueryData` directly.
      acceptAuthenticated(queryClient, authResponse({ access_token: 'token-b', user: USER_B }));
      expect(queryClient.getQueryData(currentUserQueryKey)).toEqual(USER_B);

      // The boot's answer finally arrives: 200, but for user A. The reducer is no longer `booting`
      // (B's login moved it to `authenticated`), so `BOOT_OK` is ignored.
      resolveBootFetch(jsonResponse(200, authResponse({ access_token: 'token-a', user: USER_A })));
      const result = await bootPromise;

      expect(result).toEqual({ kind: 'superseded' });

      seedFromRefresh(queryClient, result);

      // B's profile is still what the cache holds. A's was never written — seeding it would have
      // shown the wrong name in the header for whoever B is.
      expect(queryClient.getQueryData(currentUserQueryKey)).toEqual(USER_B);
    },
  );
});

describe('seedFromRefresh: a superseded result writes nothing to the cache', () => {
  it('leaves an existing ["auth","me"] entry exactly as it was', () => {
    const queryClient = makeQueryClient();
    queryClient.setQueryData(currentUserQueryKey, USER_B);

    seedFromRefresh(queryClient, { kind: 'superseded' });

    expect(queryClient.getQueryData(currentUserQueryKey)).toEqual(USER_B);
  });

  it('writes nothing when the cache had no entry at all (no phantom user appears)', () => {
    const queryClient = makeQueryClient();

    seedFromRefresh(queryClient, { kind: 'superseded' });

    expect(queryClient.getQueryData(currentUserQueryKey)).toBeUndefined();
  });
});
