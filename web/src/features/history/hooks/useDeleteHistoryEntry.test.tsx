import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { exportJobsQueryKey } from '@/features/export/hooks/useExportJobs';
import { jobPostingsKey } from '@/features/posting/hooks/useJobPostings';
import { scopeMap } from '@/features/scope/scopeMap';
import { tailoringRunQueryKey } from '@/features/tailoring/hooks/useTailoringRun';
import { USER_A, makeAccountRun, signInAs, stubAccountFetch } from '@/test/accountFetch';

import { useDeleteHistoryEntry } from './useDeleteHistoryEntry';

import type { ExportJobListResponse } from '@/features/export/types';
import type { JobPostingListResponse } from '@/features/posting/types';
import type { ReactNode } from 'react';

/**
 * Reviewer /verify round-1 MINOR #2 — RED, against the real, already-implemented
 * `useDeleteHistoryEntry` (not a skeleton): its `onSuccess` invalidates only `historyRootKey`,
 * which leaves two stale surfaces the acceptance criteria never checked.
 *
 * (a) The deleted entry may have taken its posting with it (`posting_deleted`,
 * `SqlAlchemyHistoryEntryData`'s own rule — a posting survives only while some run still references
 * it). Nothing here re-reads the response body to find out; the cheap, correct fix is to invalidate
 * the account's job-postings query too, under `accountKeyRoot(userId)` exactly as `jobPostingsKey`
 * builds it, so the account's posting picker re-reads rather than keeps showing a posting that is
 * now gone.
 *
 * (b) The run itself and its export jobs must not merely go stale — they must be gone from the
 * cache entirely. `invalidateQueries` alone would still hand a **stale but present** answer to
 * `useTailoringRun`/`useExportJobs` if either observer remounts before the network round trip
 * finishes (e.g. the back button lands on `/history/{id}/cv` between the DELETE settling and its
 * refetch), rendering a document that no longer exists anywhere. `removeQueries` is the only call
 * that closes that window, which is why the assertions below check `getQueryState(...)` — not just
 * `getQueryData(...)` — is `undefined`: a merely-invalidated entry still has a `QueryState`
 * (`isInvalidated: true`, stale data intact), and only a genuine removal clears it.
 *
 * Written from `technical-plan.md`'s cache-key builders (`jobPostingsKey`, `tailoringRunQueryKey`,
 * `exportJobsQueryKey`, all parameterized by the account `ScopeMap`), never by reading
 * `useDeleteHistoryEntry.ts` and recording what it happens to do — it does none of this today, and
 * that is exactly the gap. `gcTime: Infinity` throughout (the 2.1 timing trap CLAUDE.md names): an
 * absence has to be `onSuccess`'s doing, not the garbage collector's.
 */

const RUN_ID = 'run-1';

function makeQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
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
  vi.restoreAllMocks();
  __resetForTests();
});

describe('useDeleteHistoryEntry — cache hygiene on success (reviewer MINOR #2)', () => {
  it('invalidates the account job postings and removes the deleted run and its exports from the cache', async () => {
    stubAccountFetch({
      'DELETE /api/me/tailoring-runs/:id': () => new Response(null, { status: 204 }),
    });
    const queryClient = makeQueryClient();
    const map = scopeMap({ kind: 'account', userId: USER_A.id });

    const postingsKey = jobPostingsKey(map);
    const runKey = tailoringRunQueryKey(RUN_ID, map);
    const exportsKey = exportJobsQueryKey(RUN_ID, map);
    queryClient.setQueryData<JobPostingListResponse>(postingsKey, { items: [] });
    queryClient.setQueryData(runKey, makeAccountRun({ id: RUN_ID }));
    queryClient.setQueryData<ExportJobListResponse>(exportsKey, { items: [] });

    const { result } = renderHook(() => useDeleteHistoryEntry(USER_A.id), {
      wrapper: wrapperFor(queryClient),
    });

    await act(async () => {
      await result.current.mutateAsync(RUN_ID);
    });

    // (a) the account's job postings must be invalidated, not left to answer from a stale cache —
    // `isInvalidated` is read directly off the query's own state (2.2's technique for "was
    // invalidateQueries actually called with this key", independent of whether any component is
    // observing it right now).
    await waitFor(() => {
      expect(queryClient.getQueryState(postingsKey)?.isInvalidated).toBe(true);
    });

    // (b) the deleted run and its exports must be REMOVED, not merely marked stale: both
    // getQueryData and getQueryState come back undefined, which only a real removeQueries produces.
    expect(queryClient.getQueryData(runKey)).toBeUndefined();
    expect(queryClient.getQueryState(runKey)).toBeUndefined();
    expect(queryClient.getQueryData(exportsKey)).toBeUndefined();
    expect(queryClient.getQueryState(exportsKey)).toBeUndefined();
  });
});
