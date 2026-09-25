import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { jsonResponse } from '@/test/fixtures';

import { authQueryKeyPrefix, currentUserQueryKey } from '@/features/auth/hooks/authCache';
import { __resetForTests, authStore } from '@/features/auth/authStore';

import { savedBaseCvsQueryKey } from './savedCvsKeys';
import { useSavedBaseCvs } from './useSavedBaseCvs';

import type { SavedBaseCv } from '../types';
import type { AuthenticatedResponse, User } from '@/features/auth/types';
import type { ReactNode } from 'react';

/**
 * T26 RED — AC-33, against feature-spec.md and technical-plan.md §7, never against
 * `useSavedBaseCvs.ts`'s T25 skeleton (a permanently disabled, always-rejecting query). Every
 * assertion below therefore fails on "no data arrived" / "fetch was never called", never on an
 * `ImportError` — the hook, the key function and the auth-cache exports it needs all already exist.
 *
 * The one exception, named rather than hidden: the "lives under the auth prefix" test at the bottom
 * exercises only `savedBaseCvsQueryKey` (real, non-skeleton code in `savedCvsKeys.ts`) against
 * `QueryClient.removeQueries`, so it is expected to pass today — it is still worth recording,
 * because it is the structural half of AC-33's promise ("the `['auth']` prefix 2.1's `removeQueries`
 * already clears") and a future rename of the key away from that prefix should turn it red.
 */

function makeQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
}

function wrapperFor(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }): React.JSX.Element {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

const USER_A: User = { id: 'user-a', email: 'a@example.com', created_at: '2026-09-25T10:00:00Z' };
const USER_B: User = { id: 'user-b', email: 'b@example.com', created_at: '2026-09-25T10:00:00Z' };

function authResponseFor(user: User): AuthenticatedResponse {
  return { access_token: `token-${user.id}`, token_type: 'Bearer', expires_in: 900, user };
}

function makeSavedCv(overrides: Partial<SavedBaseCv> = {}): SavedBaseCv {
  return {
    id: 'saved-cv-a',
    label: null,
    original_filename: 'a-resume.pdf',
    content_type: 'application/pdf',
    size_bytes: 2048,
    status: 'extracted',
    character_count: 512,
    failure_reason: null,
    failure_message: null,
    uploaded_at: '2026-09-25T10:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('useSavedBaseCvs (AC-33)', () => {
  it('is disabled — no fetch at all — while the auth store is anonymous', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    authStore.signOut('expired');
    const queryClient = makeQueryClient();

    const { result } = renderHook(() => useSavedBaseCvs(), { wrapper: wrapperFor(queryClient) });

    expect(result.current.fetchStatus).toBe('idle');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('is disabled while booting — no user id is known yet', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const queryClient = makeQueryClient();
    // A fresh store defaults to `booting`; nobody has called bootstrap()/setAuthenticated() yet.

    const { result } = renderHook(() => useSavedBaseCvs(), { wrapper: wrapperFor(queryClient) });

    expect(result.current.fetchStatus).toBe('idle');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it(
    'fetches GET /api/me/base-cvs under ["auth","savedBaseCvs",userId] once authenticated with a ' +
      'known user id',
    async () => {
      const queryClient = makeQueryClient();
      queryClient.setQueryData(currentUserQueryKey, USER_A);
      authStore.setAuthenticated(authResponseFor(USER_A));
      const cvA = makeSavedCv();
      vi.stubGlobal(
        'fetch',
        vi.fn((input: string | URL): Promise<Response> => {
          const url = typeof input === 'string' ? input : input.toString();
          if (url === '/api/auth/me') {
            return Promise.resolve(jsonResponse(200, USER_A));
          }
          if (url === '/api/me/base-cvs') {
            return Promise.resolve(jsonResponse(200, { items: [cvA] }));
          }
          return Promise.reject(new Error(`unhandled fetch: ${url}`));
        }),
      );

      const { result } = renderHook(() => useSavedBaseCvs(), { wrapper: wrapperFor(queryClient) });

      await waitFor(() => {
        expect(result.current.data?.items).toEqual([cvA]);
      });
      expect(queryClient.getQueryData(savedBaseCvsQueryKey(USER_A.id))).toEqual({ items: [cvA] });
    },
  );

  it(
    "AC-33's own scenario: signing out and back in as a different user, in the same tab, never " +
      "renders the first user's list — proven by key isolation, not by removal timing " +
      "(gcTime: Infinity keeps A's entry alive throughout)",
    async () => {
      const queryClient = makeQueryClient();
      let currentUser: User = USER_A;
      const itemsByUser: Record<string, readonly SavedBaseCv[]> = {
        [USER_A.id]: [makeSavedCv({ id: 'saved-cv-a', original_filename: 'a-resume.pdf' })],
        [USER_B.id]: [makeSavedCv({ id: 'saved-cv-b', original_filename: 'b-resume.pdf' })],
      };
      vi.stubGlobal(
        'fetch',
        vi.fn((input: string | URL): Promise<Response> => {
          const url = typeof input === 'string' ? input : input.toString();
          if (url === '/api/auth/me') {
            return Promise.resolve(jsonResponse(200, currentUser));
          }
          if (url === '/api/me/base-cvs') {
            return Promise.resolve(jsonResponse(200, { items: itemsByUser[currentUser.id] ?? [] }));
          }
          return Promise.reject(new Error(`unhandled fetch: ${url}`));
        }),
      );

      // --- Signed in as A: the list loads and is cached under A's key. ---
      queryClient.setQueryData(currentUserQueryKey, USER_A);
      authStore.setAuthenticated(authResponseFor(USER_A));
      const { result, rerender } = renderHook(() => useSavedBaseCvs(), {
        wrapper: wrapperFor(queryClient),
      });
      await waitFor(() => {
        expect(result.current.data?.items.map((cv) => cv.id)).toEqual(['saved-cv-a']);
      });

      // --- A signs out, B signs in — same tab, same render tree. ---
      authStore.signOut('expired');
      currentUser = USER_B;
      queryClient.setQueryData(currentUserQueryKey, USER_B);
      authStore.setAuthenticated(authResponseFor(USER_B));
      rerender();

      await waitFor(() => {
        expect(result.current.data?.items.map((cv) => cv.id)).toEqual(['saved-cv-b']);
      });
      // A's entry is still sitting in the cache (nothing removed it in this test) — so the reason
      // B never saw A's CV is that B's query reads a different key, not that something raced to
      // delete A's in time.
      expect(queryClient.getQueryData(savedBaseCvsQueryKey(USER_A.id))).toEqual({
        items: itemsByUser[USER_A.id],
      });
      expect(result.current.data?.items.some((cv) => cv.id === 'saved-cv-a')).toBe(false);
    },
  );
});

describe('AC-33: the key lives under the auth prefix', () => {
  it("so 2.1's removeQueries(['auth']) — logout, account deletion, cross-tab sign-out — clears it too", () => {
    const queryClient = makeQueryClient();
    queryClient.setQueryData(savedBaseCvsQueryKey(USER_A.id), { items: [makeSavedCv()] });

    queryClient.removeQueries({ queryKey: authQueryKeyPrefix });

    expect(queryClient.getQueryData(savedBaseCvsQueryKey(USER_A.id))).toBeUndefined();
  });
});
