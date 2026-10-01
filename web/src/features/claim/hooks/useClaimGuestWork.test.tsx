import { QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { savedBaseCvsQueryKey } from '@/features/savedCvs/hooks/savedCvsKeys';
import {
  USER_A,
  bearerFor,
  callsTo,
  hang,
  ok,
  signInAs,
  status,
  stubAccountFetch,
} from '@/test/accountFetch';

import { CLAIM_PATH, claimResult, newClient } from '../test/support';
import { claimGuestWorkMutationKey, useClaimGuestWork } from './useClaimGuestWork';

import type { QueryClient } from '@tanstack/react-query';
import type { ReactNode } from 'react';

/**
 * T30 RED — the claim mutation (AC-38, AC-42). Against T29's skeleton `mutate()` rejects without a
 * request and without cache work, and `claimGuestWorkMutationKey` is a `[...accountKeyRoot, …]`
 * that the skeleton's `useMutation` never receives — so each test is red on its assertion.
 */

function seedGuestAndAccount(queryClient: QueryClient): void {
  queryClient.setQueryData(['intake', 'baseCvs'], { items: ['seed'] });
  queryClient.setQueryData(['posting', 'jobPostings'], { items: ['seed'] });
  queryClient.setQueryData(['tailoring', 'tailoringRuns'], { items: ['seed'] });
  queryClient.setQueryData(['tailoring', 'tailoringRun', 'r1'], { id: 'r1' });
  queryClient.setQueryData(['export', 'exportJobs', 'r1'], { items: ['seed'] });
  queryClient.setQueryData(['auth', 'account', USER_A.id, 'history'], { items: ['seed'] });
  queryClient.setQueryData(savedBaseCvsQueryKey(USER_A.id), { items: ['seed'] });
}

function wrapperFor(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }): React.JSX.Element {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('claimGuestWorkMutationKey (AC-42)', () => {
  it('lives under the account root, so a sign-out that removes [auth] owns it', () => {
    expect(claimGuestWorkMutationKey('user-a')).toEqual([
      'auth',
      'account',
      'user-a',
      'claimGuestWork',
    ]);
  });

  it('differs per user', () => {
    expect(claimGuestWorkMutationKey('user-a')).not.toEqual(claimGuestWorkMutationKey('user-b'));
  });
});

describe('useClaimGuestWork', () => {
  it('posts once to the claim route with the bearer', async () => {
    const fetch = stubAccountFetch({ [`POST ${CLAIM_PATH}`]: ok(claimResult()) });
    const queryClient = newClient();
    const { result } = renderHook(() => useClaimGuestWork(USER_A.id), {
      wrapper: wrapperFor(queryClient),
    });

    act(() => {
      result.current.mutate();
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    const posts = callsTo(fetch, 'POST', CLAIM_PATH);
    expect(posts).toHaveLength(1);
    expect(posts[0]?.authorization).toBe(bearerFor(USER_A));
    expect(result.current.data).toEqual(claimResult());
  });

  it('registers its mutation under the account-rooted key while in flight', async () => {
    stubAccountFetch({ [`POST ${CLAIM_PATH}`]: hang });
    const queryClient = newClient();
    const { result } = renderHook(() => useClaimGuestWork(USER_A.id), {
      wrapper: wrapperFor(queryClient),
    });

    act(() => {
      result.current.mutate();
    });

    await waitFor(() => {
      expect(
        queryClient.isMutating({ mutationKey: ['auth', 'account', USER_A.id, 'claimGuestWork'] }),
      ).toBe(1);
    });
    expect(queryClient.isMutating({ mutationKey: ['auth', 'account', 'someone-else'] })).toBe(0);
  });

  it('on success removes every guest query root and keeps nothing of the guest session', async () => {
    stubAccountFetch({ [`POST ${CLAIM_PATH}`]: ok(claimResult()) });
    const queryClient = newClient();
    seedGuestAndAccount(queryClient);
    expect(queryClient.getQueryData(['export', 'exportJobs', 'r1'])).toBeDefined();
    const { result } = renderHook(() => useClaimGuestWork(USER_A.id), {
      wrapper: wrapperFor(queryClient),
    });

    act(() => {
      result.current.mutate();
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    for (const root of ['intake', 'posting', 'tailoring', 'export']) {
      expect(queryClient.getQueryCache().findAll({ queryKey: [root] })).toHaveLength(0);
    }
  });

  it('on success invalidates the account root', async () => {
    stubAccountFetch({ [`POST ${CLAIM_PATH}`]: ok(claimResult()) });
    const queryClient = newClient();
    seedGuestAndAccount(queryClient);
    expect(
      queryClient.getQueryState(['auth', 'account', USER_A.id, 'history'])?.isInvalidated,
    ).toBe(false);
    const { result } = renderHook(() => useClaimGuestWork(USER_A.id), {
      wrapper: wrapperFor(queryClient),
    });

    act(() => {
      result.current.mutate();
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(
      queryClient.getQueryState(['auth', 'account', USER_A.id, 'history'])?.isInvalidated,
    ).toBe(true);
  });

  it('on success also refreshes the saved-CV list, which lives OUTSIDE the account root', async () => {
    // ['auth','savedBaseCvs',userId] is not under ['auth','account',userId]: invalidating the
    // account root alone would leave the claimed CVs missing from the picker and /account.
    stubAccountFetch({ [`POST ${CLAIM_PATH}`]: ok(claimResult()) });
    const queryClient = newClient();
    seedGuestAndAccount(queryClient);
    const { result } = renderHook(() => useClaimGuestWork(USER_A.id), {
      wrapper: wrapperFor(queryClient),
    });

    act(() => {
      result.current.mutate();
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    const saved = queryClient.getQueryState(savedBaseCvsQueryKey(USER_A.id));
    expect(saved === undefined || saved.isInvalidated).toBe(true);
  });

  it('on failure touches no cache: nothing moved, so nothing is forgotten', async () => {
    const fetch = stubAccountFetch({
      [`POST ${CLAIM_PATH}`]: status(503, 'service_unavailable'),
    });
    const queryClient = newClient();
    seedGuestAndAccount(queryClient);
    const { result } = renderHook(() => useClaimGuestWork(USER_A.id), {
      wrapper: wrapperFor(queryClient),
    });

    act(() => {
      result.current.mutate();
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(callsTo(fetch, 'POST', CLAIM_PATH)).toHaveLength(1);
    expect(queryClient.getQueryData(['intake', 'baseCvs'])).toBeDefined();
    expect(
      queryClient.getQueryState(['auth', 'account', USER_A.id, 'history'])?.isInvalidated,
    ).toBe(false);
  });
});
