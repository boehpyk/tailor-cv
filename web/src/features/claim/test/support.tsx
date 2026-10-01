import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router';

import { USER_A, ok, signedInRoutes } from '@/test/accountFetch';
import { makeExtractedCv, makeRunSummary } from '@/test/fixtures';

import { GuestWorkOffer } from '../components/GuestWorkOffer';

import type { RouteHandler } from '@/test/accountFetch';
import type { BaseCv } from '@/features/intake/types';
import type { TailoringRunSummary } from '@/features/tailoring/types';
import type { GuestWorkClaimResult } from '../types';
import type { RenderResult } from '@testing-library/react';

/**
 * Slice 2.4 test support (T30): fixtures and a renderer shared by the claim's component tests.
 *
 * **The client keeps what it is given.** `gcTime: Infinity`, because `gcTime: 0` garbage-collects
 * an *unobserved* `setQueryData` entry on the next macrotask — an "it was removed" assertion on one
 * would then test the collector, not the claim (CLAUDE.md, 2.1's trap; AC-42).
 */

export const CLAIM_PATH = '/api/me/guest-work/claim';

export function newClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  });
}

export function guestCv(overrides: Partial<BaseCv> = {}): BaseCv {
  return makeExtractedCv({ id: 'guest-cv-1', original_filename: 'jane.pdf', ...overrides });
}

/** A working copy (2.2): the guest-side stand-in for a saved CV, which a claim never moves. */
export function workingCopy(overrides: Partial<BaseCv> = {}): BaseCv {
  return guestCv({
    id: 'guest-copy-1',
    original_filename: 'working-copy.pdf',
    origin: 'copied_from_saved',
    ...overrides,
  });
}

export function guestRun(overrides: Partial<TailoringRunSummary> = {}): TailoringRunSummary {
  return makeRunSummary({ id: 'guest-run-1', status: 'succeeded', ...overrides });
}

export function claimResult(overrides: Partial<GuestWorkClaimResult> = {}): GuestWorkClaimResult {
  return {
    base_cvs: 1,
    job_postings: 1,
    tailoring_runs: 2,
    export_jobs: 0,
    working_copies_dropped: 0,
    ...overrides,
  };
}

/** What a signed-in browser's guest lists answer — both **without** the bearer (AC-37). */
export function guestListRoutes(
  cvs: readonly BaseCv[],
  runs: readonly TailoringRunSummary[],
): Record<string, RouteHandler> {
  return {
    ...signedInRoutes(USER_A),
    'GET /api/base-cvs': ok({ items: cvs }),
    'GET /api/tailoring-runs': ok({ items: runs }),
  };
}

export interface RenderOfferOptions {
  readonly queryClient?: QueryClient;
  readonly userId?: string;
  readonly onClaimed?: (result: GuestWorkClaimResult) => void;
  /** Extra tree rendered beside the offer (a probe reading the account's saved CVs, say). */
  readonly beside?: React.ReactNode;
}

export function renderOffer(opts: RenderOfferOptions = {}): RenderResult & {
  readonly queryClient: QueryClient;
} {
  const queryClient = opts.queryClient ?? newClient();
  const result = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <GuestWorkOffer
          userId={opts.userId ?? USER_A.id}
          {...(opts.onClaimed ? { onClaimed: opts.onClaimed } : {})}
        />
        {opts.beside}
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, queryClient };
}
