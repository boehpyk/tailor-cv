import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { previewPurge, previewSweep, runPurge, runSweep } from '@/api/admin';
import { readinessQueryKey } from '@/features/health/hooks/useReadiness';

import { adminKey } from './useAdminUser';
import { guestListKey } from './useGuestSessions';
import { storageKey } from './useStorage';
import { isClientError } from './useUserDirectory';

import type { PurgeReport, SweepReport } from '@/api/admin';
import type { QueryClient, UseMutationResult, UseQueryResult } from '@tanstack/react-query';

export type RetentionJob = 'purge' | 'sweep';

export function retentionPreviewKey(actorId: string, job: RetentionJob): readonly string[] {
  return [...adminKey(actorId), 'preview', job];
}

/** A caller guards a same-tick double click with `isMutating({ mutationKey })` (2.1). */
export function retentionRunMutationKey(job: RetentionJob): readonly string[] {
  return ['auth', 'account', 'admin', 'retentionRun', job];
}

export interface RetentionRuns {
  /** `enabled: false`: nothing is asked until *Preview* calls `refetch` (AC-37, AC-38). */
  readonly purgePreview: UseQueryResult<PurgeReport>;
  readonly sweepPreview: UseQueryResult<SweepReport>;
  /** 409 `purge_in_progress` and 503s are the mutation's `error`; never re-sent (ADR-0031). */
  readonly purge: UseMutationResult<PurgeReport, Error, void>;
  readonly sweep: UseMutationResult<SweepReport, Error, void>;
}

function retryOnce(failureCount: number, error: Error): boolean {
  return !isClientError(error) && failureCount < 1;
}

/**
 * After a run, what it changed: the job facts, the volume, the list — and its now-stale preview.
 * Fired, not awaited: a returned promise keeps the mutation pending until every refetch lands, and
 * `/health/ready` takes ~2 s (`probe_celery`), so the report would show that much late (T28).
 */
function afterRun(queryClient: QueryClient, actorId: string, job: RetentionJob): void {
  queryClient.removeQueries({ queryKey: retentionPreviewKey(actorId, job) });
  void queryClient.invalidateQueries({ queryKey: readinessQueryKey });
  void queryClient.invalidateQueries({ queryKey: storageKey(actorId) });
  void queryClient.invalidateQueries({ queryKey: guestListKey(actorId) });
}

/** The purge and the sweep from `/admin/storage`: previews and runs. Not optimistic. */
export function useRetentionRuns(actorId: string): RetentionRuns {
  const queryClient = useQueryClient();
  const purgePreview = useQuery({
    queryKey: retentionPreviewKey(actorId, 'purge'),
    queryFn: ({ signal }) => previewPurge(signal),
    enabled: false,
    retry: retryOnce,
  });
  const sweepPreview = useQuery({
    queryKey: retentionPreviewKey(actorId, 'sweep'),
    queryFn: ({ signal }) => previewSweep(signal),
    enabled: false,
    retry: retryOnce,
  });
  const purge = useMutation({
    mutationKey: retentionRunMutationKey('purge'),
    mutationFn: runPurge,
    // Settled, not success: a 503 may follow sessions already deleted (AC-37).
    onSettled: () => {
      afterRun(queryClient, actorId, 'purge');
    },
  });
  const sweep = useMutation({
    mutationKey: retentionRunMutationKey('sweep'),
    mutationFn: runSweep,
    onSettled: () => {
      afterRun(queryClient, actorId, 'sweep');
    },
  });
  return { purgePreview, sweepPreview, purge, sweep };
}
