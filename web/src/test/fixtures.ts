import { vi } from 'vitest';

import type { BaseCv } from '@/features/intake/types';
import type { JobPostingSummary } from '@/features/posting/types';
import type { TailoringRun, TailoringRunSummary } from '@/features/tailoring/types';

/**
 * Fixture factories and a `fetch` stub shared across the frontend suite, extracted from 1.3's
 * `TailorPanel.test.tsx` (task-list F5b/F6) because `WorkspacePage` and `RunPage` need the same
 * three list/detail shapes that panel's tests already built by hand, and `TailorPanel.test.tsx`
 * itself is deleted at F11 — duplicating its helpers into every new test file would mean losing them
 * on that delete rather than carrying them forward.
 *
 * Wire fixtures only: every field a real response carries, defaulted to a `succeeded`-adjacent
 * shape a test overrides from. Not a builder DSL — plain objects with `Partial<...>` overrides is
 * what every sibling panel's tests already use, and a second pattern here would be a second thing to
 * learn for no benefit.
 */

const BASE_CV_ID = '0192f0a1-aaaa-7000-8000-00000000aaaa';
const JOB_POSTING_ID = '0192f0a1-bbbb-7000-8000-00000000bbbb';
const RUN_ID = '0192f0a1-cccc-7000-8000-00000000cccc';

export function jsonResponse(
  status: number,
  body: unknown,
  headers?: Record<string, string>,
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

export function makeExtractedCv(overrides: Partial<BaseCv> = {}): BaseCv {
  return {
    id: BASE_CV_ID,
    original_filename: 'resume.pdf',
    content_type: 'application/pdf',
    size_bytes: 2048,
    status: 'extracted',
    character_count: 512,
    failure_reason: null,
    failure_message: null,
    uploaded_at: '2026-09-11T10:00:00Z',
    expires_at: '2026-09-12T10:00:00Z',
    ...overrides,
  };
}

export function makePostingSummary(overrides: Partial<JobPostingSummary> = {}): JobPostingSummary {
  return {
    id: JOB_POSTING_ID,
    source: 'pasted',
    source_url: null,
    title: 'Senior Python Engineer',
    character_count: 4321,
    preview: 'We are looking for a senior Python engineer…',
    created_at: '2026-09-11T10:00:00Z',
    expires_at: '2026-09-12T10:00:00Z',
    ...overrides,
  };
}

export function makeRunSummary(overrides: Partial<TailoringRunSummary> = {}): TailoringRunSummary {
  return {
    id: RUN_ID,
    status: 'queued',
    base_cv_id: BASE_CV_ID,
    job_posting_id: JOB_POSTING_ID,
    failure_reason: null,
    retryable: false,
    tailored_cv_character_count: null,
    cover_letter_character_count: null,
    model: null,
    prompt_version: null,
    llm_duration_ms: null,
    requested_at: '2026-09-12T10:00:00Z',
    started_at: null,
    completed_at: null,
    expires_at: '2026-09-13T10:00:00Z',
    version: 1,
    tailored_cv_edited_at: null,
    cover_letter_edited_at: null,
    ...overrides,
  };
}

export function makeRun(overrides: Partial<TailoringRun> = {}): TailoringRun {
  return {
    id: RUN_ID,
    status: 'queued',
    base_cv_id: BASE_CV_ID,
    job_posting_id: JOB_POSTING_ID,
    failure_reason: null,
    retryable: false,
    tailored_cv: null,
    cover_letter: null,
    tailored_cv_character_count: null,
    cover_letter_character_count: null,
    model: null,
    prompt_version: null,
    llm_duration_ms: null,
    requested_at: '2026-09-12T10:00:00Z',
    started_at: null,
    completed_at: null,
    expires_at: '2026-09-13T10:00:00Z',
    version: 1,
    tailored_cv_edited_at: null,
    cover_letter_edited_at: null,
    ...overrides,
  };
}

export interface WorkspaceStubs {
  readonly baseCvs?: () => Promise<Response>;
  readonly jobPostings?: () => Promise<Response>;
  readonly runsList?: () => Promise<Response>;
  readonly createRun?: () => Promise<Response>;
  /** Keyed by run id. Each handler receives the 1-based call number for THAT id. */
  readonly runDetail?: Record<string, (callNumber: number) => Promise<Response>>;
  /**
   * `GET /api/tailoring-runs/{id}/exports` — the export bar's own list query, keyed by run id like
   * `runDetail`. **Defaults to a resolved `{ items: [] }`, not an unhandled-call rejection**: F8
   * found that every `RunPage` test whose run reaches `succeeded` mounts `ExportBar`
   * (`RunPage.tsx` renders it above `DocumentWorkspace` on that one status), and before this
   * default existed every such test silently exercised the bar's *"We couldn't check your
   * downloads"* error state instead of the idle one, because the call fell through to this stub's
   * catch-all rejection. A test that wants a specific export list — jobs in flight, an error, a
   * particular byte size — still overrides this per run id.
   *
   * **Resolved, not "fall through and let the caller supply it"**: `useExportJobs`'s `retry` is a
   * function, not `false` (its own docstring says so — it overrides the test client's default), so
   * an unstubbed rejection here would retry three times with backoff and could leave a timer
   * in flight after a test that never awaited it ends.
   */
  readonly exports?: Record<string, (callNumber: number) => Promise<Response>>;
}

/**
 * A `fetch` stub routing on URL and method, covering the six endpoints the workspace and the run
 * page read between them: the two intake/posting lists (unchanged since 1.1/1.2), the run list, run
 * creation, one run's detail while polling, and one run's export list. Each stub defaults to an
 * empty/never-called response so a test only wires the endpoints it actually exercises, and an
 * unhandled call rejects loudly rather than hanging — the same shape `TailorPanel.test.tsx`'s
 * `stubFetch` used, generalised beyond one panel's needs.
 */
export function stubWorkspaceFetch(stubs: WorkspaceStubs): ReturnType<typeof vi.fn> {
  const detailCallCounts = new Map<string, number>();
  const exportsCallCounts = new Map<string, number>();
  const fetchMock = vi.fn((input: string | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = init?.method ?? 'GET';

    if (url === '/api/base-cvs') {
      return (stubs.baseCvs ?? (() => Promise.resolve(jsonResponse(200, { items: [] }))))();
    }
    if (url === '/api/job-postings') {
      return (stubs.jobPostings ?? (() => Promise.resolve(jsonResponse(200, { items: [] }))))();
    }
    if (url === '/api/tailoring-runs' && method === 'POST') {
      if (stubs.createRun === undefined) {
        return Promise.reject(new Error('unexpected POST /api/tailoring-runs in this test'));
      }
      return stubs.createRun();
    }
    if (url === '/api/tailoring-runs') {
      return (stubs.runsList ?? (() => Promise.resolve(jsonResponse(200, { items: [] }))))();
    }
    const detailMatch = /^\/api\/tailoring-runs\/([^/]+)$/.exec(url);
    if (detailMatch) {
      const id = decodeURIComponent(detailMatch[1] ?? '');
      const handler = stubs.runDetail?.[id];
      if (handler === undefined) {
        return Promise.reject(new Error(`unexpected GET ${url} in this test`));
      }
      const callNumber = (detailCallCounts.get(id) ?? 0) + 1;
      detailCallCounts.set(id, callNumber);
      return handler(callNumber);
    }
    const exportsMatch = /^\/api\/tailoring-runs\/([^/]+)\/exports$/.exec(url);
    if (exportsMatch) {
      const id = decodeURIComponent(exportsMatch[1] ?? '');
      const handler = stubs.exports?.[id];
      if (handler === undefined) {
        // The documented default (see `WorkspaceStubs.exports`'s docstring): resolved, not
        // rejected, and scoped to this one path — never a catch-all, which would also answer
        // `/health/ready` and take `SystemStatus` down through the route tree's error boundary.
        return Promise.resolve(jsonResponse(200, { items: [] }));
      }
      const callNumber = (exportsCallCounts.get(id) ?? 0) + 1;
      exportsCallCounts.set(id, callNumber);
      return handler(callNumber);
    }
    return Promise.reject(new Error(`unhandled fetch: ${method} ${url}`));
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/**
 * Count calls to `path` by HTTP method (default `GET`). Method-aware because `/api/tailoring-runs`
 * is both the list `GET` and the create `POST` — counting the URL alone cannot tell "no run was
 * created" apart from "the list loaded as usual" (the same trap `TailorPanel.test.tsx` named).
 */
export function countCallsTo(
  fetchMock: ReturnType<typeof vi.fn>,
  path: string,
  method: 'GET' | 'POST' | 'PUT' = 'GET',
): number {
  // `vi.fn()`'s calls are `any[][]`; narrow each tuple by hand rather than annotating the
  // parameter, which tsc rejects as a tuple that "may have fewer" elements.
  return fetchMock.mock.calls.filter((call) => {
    const [input, init] = call as [unknown, RequestInit | undefined];
    const url = typeof input === 'string' ? input : String(input);
    const callMethod = init?.method ?? 'GET';
    return url === path && callMethod === method;
  }).length;
}
