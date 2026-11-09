import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { WorkspaceScopeProvider } from '@/features/scope/WorkspaceScope';
import { useCreateTailoringRun } from '@/features/tailoring/hooks/useCreateTailoringRun';
import { useTailoringRun } from '@/features/tailoring/hooks/useTailoringRun';
import { createAppQueryClient } from '@/queryClient';
import { USER_A, USER_B, hang, signInAs, status, stubAccountFetch } from '@/test/accountFetch';
import { jsonResponse, makeRun } from '@/test/fixtures';

import { creditsQueryKey } from './billingKeys';
import { usePackClick } from './usePackClick';
import { usePaymentNotice } from './usePaymentNotice';

import type { Credits } from '../types';
import type { ReactNode } from 'react';

/**
 * T33 RED (slice 5.2; plan §5's data table): the hook-level rules the component states lean on.
 * Most of these hold against T31's hooks already and are recorded as **guards** (they pin the rules
 * T34/T35 must not break): pack and opt-in writes land only in an existing credits read, a 409
 * `no_credits` and the poll that first sees a terminal run both invalidate it.
 */

const CREDITS: Credits = {
  balance: 2,
  out_of_credits: false,
  notice: null,
  payment_notice_opted_in: false,
  packs: [],
  entries: [],
};

let queryClient: QueryClient;

function wrapper({ children }: { children: ReactNode }): React.JSX.Element {
  return (
    <QueryClientProvider client={queryClient}>
      <WorkspaceScopeProvider scope={{ kind: 'account', userId: USER_A.id }}>
        {children}
      </WorkspaceScopeProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  __resetForTests();
  signInAs(USER_A);
  queryClient = createAppQueryClient();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('pack and opt-in writes', () => {
  it('a pack click writes its answer into an existing credits read', async () => {
    stubAccountFetch({
      'POST /api/me/pack-clicks': () => jsonResponse(200, { payment_notice_opted_in: true }),
    });
    queryClient.setQueryData(creditsQueryKey(USER_A.id), CREDITS);
    const { result } = renderHook(() => usePackClick(USER_A.id), { wrapper });
    await act(async () => {
      await result.current.mutateAsync('pack_3');
    });
    expect(
      queryClient.getQueryData<Credits>(creditsQueryKey(USER_A.id))?.payment_notice_opted_in,
    ).toBe(true);
  });

  it('a pack click creates no credits read when none exists', async () => {
    stubAccountFetch({
      'POST /api/me/pack-clicks': () => jsonResponse(200, { payment_notice_opted_in: true }),
    });
    const { result } = renderHook(() => usePackClick(USER_A.id), { wrapper });
    await act(async () => {
      await result.current.mutateAsync('pack_3');
    });
    expect(queryClient.getQueryData(creditsQueryKey(USER_A.id))).toBeUndefined();
    expect(
      queryClient.getQueryCache().find({ queryKey: creditsQueryKey(USER_A.id) }),
    ).toBeUndefined();
  });

  it('an opt-in writes into an existing read, and into no other account', async () => {
    stubAccountFetch({
      'PUT /api/me/payment-notice': () => jsonResponse(200, { payment_notice_opted_in: true }),
    });
    queryClient.setQueryData(creditsQueryKey(USER_A.id), CREDITS);
    queryClient.setQueryData(creditsQueryKey(USER_B.id), CREDITS);
    const { result } = renderHook(() => usePaymentNotice(USER_A.id), { wrapper });
    await act(async () => {
      await result.current.mutateAsync(true);
    });
    expect(
      queryClient.getQueryData<Credits>(creditsQueryKey(USER_A.id))?.payment_notice_opted_in,
    ).toBe(true);
    expect(
      queryClient.getQueryData<Credits>(creditsQueryKey(USER_B.id))?.payment_notice_opted_in,
    ).toBe(false);
  });

  it('an opt-in answered after the read was removed does not bring it back', async () => {
    let release: (r: Response) => void = () => undefined;
    stubAccountFetch({
      'PUT /api/me/payment-notice': () => new Promise<Response>((resolve) => (release = resolve)),
    });
    queryClient.setQueryData(creditsQueryKey(USER_A.id), CREDITS);
    const { result } = renderHook(() => usePaymentNotice(USER_A.id), { wrapper });
    act(() => {
      result.current.mutate(true);
    });
    await waitFor(() => {
      expect(result.current.isPending).toBe(true);
    });
    queryClient.removeQueries({ queryKey: creditsQueryKey(USER_A.id), exact: true });
    await act(async () => {
      release(jsonResponse(200, { payment_notice_opted_in: true }));
      await waitFor(() => {
        expect(result.current.isSuccess).toBe(true);
      });
    });
    expect(
      queryClient.getQueryCache().find({ queryKey: creditsQueryKey(USER_A.id) }),
    ).toBeUndefined();
  });

  it('a failed opt-in leaves the read unchanged', async () => {
    stubAccountFetch({ 'PUT /api/me/payment-notice': status(503, 'service_unavailable') });
    queryClient.setQueryData(creditsQueryKey(USER_A.id), CREDITS);
    const { result } = renderHook(() => usePaymentNotice(USER_A.id), { wrapper });
    await act(async () => {
      await result.current.mutateAsync(true).catch(() => undefined);
    });
    expect(queryClient.getQueryData(creditsQueryKey(USER_A.id))).toEqual(CREDITS);
  });
});

describe('credits invalidation', () => {
  it('a 409 no_credits on the launch invalidates the credits read', async () => {
    stubAccountFetch({ 'POST /api/me/tailoring-runs': status(409, 'no_credits') });
    queryClient.setQueryData(creditsQueryKey(USER_A.id), CREDITS);
    const { result } = renderHook(() => useCreateTailoringRun(), { wrapper });
    await act(async () => {
      await result.current
        .mutateAsync({ base_cv_id: 'cv', job_posting_id: 'jp' })
        .catch(() => undefined);
    });
    expect(queryClient.getQueryState(creditsQueryKey(USER_A.id))?.isInvalidated).toBe(true);
  });

  it('a 503 queue_unavailable on the launch invalidates the credits read', async () => {
    stubAccountFetch({ 'POST /api/me/tailoring-runs': status(503, 'queue_unavailable') });
    queryClient.setQueryData(creditsQueryKey(USER_A.id), CREDITS);
    const { result } = renderHook(() => useCreateTailoringRun(), { wrapper });
    await act(async () => {
      await result.current
        .mutateAsync({ base_cv_id: 'cv', job_posting_id: 'jp' })
        .catch(() => undefined);
    });
    expect(queryClient.getQueryState(creditsQueryKey(USER_A.id))?.isInvalidated).toBe(true);
  });

  it('a 409 for any other reason leaves the credits read alone', async () => {
    stubAccountFetch({ 'POST /api/me/tailoring-runs': status(409, 'run_in_progress') });
    queryClient.setQueryData(creditsQueryKey(USER_A.id), CREDITS);
    const { result } = renderHook(() => useCreateTailoringRun(), { wrapper });
    await act(async () => {
      await result.current
        .mutateAsync({ base_cv_id: 'cv', job_posting_id: 'jp' })
        .catch(() => undefined);
    });
    expect(queryClient.getQueryState(creditsQueryKey(USER_A.id))?.isInvalidated).toBe(false);
  });

  it('the poll that first sees a terminal run invalidates credits', async () => {
    stubAccountFetch({
      'GET /api/me/tailoring-runs/:id': () =>
        jsonResponse(200, makeRun({ status: 'failed', expires_at: null })),
    });
    queryClient.setQueryData(creditsQueryKey(USER_A.id), CREDITS);
    const { result } = renderHook(() => useTailoringRun('run-1'), { wrapper });
    await waitFor(() => {
      expect(result.current.data?.status).toBe('failed');
    });
    expect(queryClient.getQueryState(creditsQueryKey(USER_A.id))?.isInvalidated).toBe(true);
  });

  it('a later read of an already-terminal run invalidates nothing', async () => {
    stubAccountFetch({
      'GET /api/me/tailoring-runs/:id': () =>
        jsonResponse(200, makeRun({ status: 'succeeded', expires_at: null })),
    });
    const { result } = renderHook(() => useTailoringRun('run-1'), { wrapper });
    await waitFor(() => {
      expect(result.current.data?.status).toBe('succeeded');
    });
    queryClient.setQueryData(creditsQueryKey(USER_A.id), CREDITS);
    await act(async () => {
      await result.current.refetch();
    });
    expect(queryClient.getQueryState(creditsQueryKey(USER_A.id))?.isInvalidated).toBe(false);
  });

  it('a still-running poll invalidates nothing', async () => {
    stubAccountFetch({
      'GET /api/me/tailoring-runs/:id': () =>
        jsonResponse(200, makeRun({ status: 'running', expires_at: null })),
      'GET /api/me/credits': hang,
    });
    queryClient.setQueryData(creditsQueryKey(USER_A.id), CREDITS);
    const { result } = renderHook(() => useTailoringRun('run-1'), { wrapper });
    await waitFor(() => {
      expect(result.current.data?.status).toBe('running');
    });
    expect(queryClient.getQueryState(creditsQueryKey(USER_A.id))?.isInvalidated).toBe(false);
  });
});
