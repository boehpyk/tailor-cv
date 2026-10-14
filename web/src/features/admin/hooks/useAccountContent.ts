import { useInfiniteQuery, useQuery } from '@tanstack/react-query';

import {
  fetchUserHistoryPage,
  getUserCv,
  getUserPosting,
  getUserRun,
  listUserCvs,
  listUserPostings,
  listUserRunExports,
} from '@/api/admin';

import { subjectKey } from './useAdminSubject';
import { isClientError } from './useUserDirectory';

import type { AdminSavedBaseCv, AdminSubject } from '@/api/admin';
import type { ExportJobListResponse } from '@/features/export/types';
import type { HistoryPage } from '@/features/history/types';
import type { JobPosting, JobPostingListResponse } from '@/features/posting/types';
import type { SavedBaseCvList } from '@/features/savedCvs/types';
import type { TailoringRun } from '@/features/tailoring/types';
import type { InfiniteData, UseInfiniteQueryResult, UseQueryResult } from '@tanstack/react-query';

/**
 * Everything an admin read of one account's content, **under that account's detail key** (AC-32):
 * 4.2's erase removes the detail prefix and so drops every body loaded here, and a sign-out drops
 * the actor's whole account root.
 *
 * Shapes: `['content', 'cvs']`, `['content', 'cv', id]`, `['content', 'postings']`,
 * `['content', 'posting', id]`, `['content', 'history']`, `['content', 'run', id]` and the run's
 * exports at `['content', 'run', id, 'exports']` — under the run, so removing a run's key (a
 * delete) takes its exports with it.
 */
export function adminContentKey(
  actorId: string,
  userId: string,
  ...parts: readonly string[]
): readonly string[] {
  return subjectContentKey(actorId, { kind: 'user', userId }, ...parts);
}

/** The same shapes under any subject's root; a user's is `adminContentKey`, byte for byte. */
export function subjectContentKey(
  actorId: string,
  subject: AdminSubject,
  ...parts: readonly string[]
): readonly string[] {
  return [...subjectKey(actorId, subject), 'content', ...parts];
}

/** A 4xx (gone, firewall, malformed) is an answer; a 503 or a network failure is retried once. */
function retryOnce(failureCount: number, error: Error): boolean {
  return !isClientError(error) && failureCount < 1;
}

export function useAdminCvs(actorId: string, userId: string): UseQueryResult<SavedBaseCvList> {
  return useQuery({
    queryKey: adminContentKey(actorId, userId, 'cvs'),
    queryFn: ({ signal }) => listUserCvs(userId, signal),
    retry: retryOnce,
  });
}

/** The 20 most recent postings (OQ-6) — a fixed window, no cursor. */
export function useAdminPostings(
  actorId: string,
  userId: string,
): UseQueryResult<JobPostingListResponse> {
  return useQuery({
    queryKey: adminContentKey(actorId, userId, 'postings'),
    queryFn: ({ signal }) => listUserPostings(userId, signal),
    retry: retryOnce,
  });
}

export type AdminHistoryQuery = UseInfiniteQueryResult<InfiniteData<HistoryPage, string | null>>;

/** The account's history, page by page; the server says whether there is more. */
export function useAdminHistory(actorId: string, userId: string): AdminHistoryQuery {
  return useInfiniteQuery({
    queryKey: adminContentKey(actorId, userId, 'history'),
    queryFn: ({ pageParam, signal }) => fetchUserHistoryPage(userId, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    retry: retryOnce,
  });
}

export function useAdminCv(
  actorId: string,
  subject: AdminSubject,
  baseCvId: string,
): UseQueryResult<AdminSavedBaseCv> {
  return useQuery({
    queryKey: subjectContentKey(actorId, subject, 'cv', baseCvId),
    queryFn: ({ signal }) => getUserCv(subject, baseCvId, signal),
    retry: retryOnce,
  });
}

export function useAdminPosting(
  actorId: string,
  subject: AdminSubject,
  postingId: string,
): UseQueryResult<JobPosting> {
  return useQuery({
    queryKey: subjectContentKey(actorId, subject, 'posting', postingId),
    queryFn: ({ signal }) => getUserPosting(subject, postingId, signal),
    retry: retryOnce,
  });
}

/** One run. No polling (OQ-12): a queued or running run is refreshed by the admin's click. */
export function useAdminRun(
  actorId: string,
  subject: AdminSubject,
  runId: string,
): UseQueryResult<TailoringRun> {
  return useQuery({
    queryKey: subjectContentKey(actorId, subject, 'run', runId),
    queryFn: ({ signal }) => getUserRun(subject, runId, signal),
    retry: retryOnce,
  });
}

export function useAdminRunExports(
  actorId: string,
  subject: AdminSubject,
  runId: string,
): UseQueryResult<ExportJobListResponse> {
  return useQuery({
    queryKey: subjectContentKey(actorId, subject, 'run', runId, 'exports'),
    queryFn: ({ signal }) => listUserRunExports(subject, runId, signal),
    retry: retryOnce,
  });
}
