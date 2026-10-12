import { USER_A, makeAccountRun, makeHistoryEntry, ok, signedInRoutes } from './accountFetch';
import { GUEST_HOME, adminOnly, detail, noContent, userGone } from './adminFetch';
import { jsonResponse } from './fixtures';

import type { RouteHandler } from './accountFetch';
import type { AdminHistoryEntryErasure, AdminSavedBaseCv } from '@/api/admin';
import type { User } from '@/features/auth/types';
import type { SavedBaseCv } from '@/features/savedCvs/types';
import type { ExportJob } from '@/features/export/types';
import type { HistoryEntry } from '@/features/history/types';
import type { JobPosting, JobPostingSummary } from '@/features/posting/types';
import type { TailoringRun } from '@/features/tailoring/types';

/**
 * Slice 4.3 test support (T12): a stateful fake of the eleven `/api/admin/users/{id}/…` content
 * routes, plus the fixtures the content screens read.
 *
 * **Per-bearer isolation** (2.4's `ed66fbe`): every route is wrapped in `adminOnly`, so a request
 * carrying anyone's credential but the admin's gets the firewall's code-less 404 — a client that
 * sends the wrong token fails here instead of passing against a fake that answers anyone. Deletes
 * change the world, so a later read sees what a real server would show.
 */

export const ADMIN: User = { ...USER_A, id: 'admin-1', email: 'root@example.com', role: 'admin' };
export const TARGET_ID = 'u-ann';
export const TARGET = detail({ id: TARGET_ID, email: 'ann@example.com' });

/** Everything an account holds, as the fake serves it. History is paged: page `i` is cursor `c{i}`. */
export interface ContentWorld {
  cvs: AdminSavedBaseCv[];
  postings: JobPosting[];
  historyPages: HistoryEntry[][];
  runs: TailoringRun[];
  exports: Record<string, ExportJob[]>;
}

export function savedCv(overrides: Partial<AdminSavedBaseCv> = {}): AdminSavedBaseCv {
  return {
    id: 'cv-1',
    label: 'Backend roles',
    original_filename: 'jane.pdf',
    content_type: 'application/pdf',
    size_bytes: 2048,
    status: 'extracted',
    character_count: 4200,
    failure_reason: null,
    failure_message: null,
    uploaded_at: '2026-09-20T10:00:00Z',
    extracted_text: 'Jane Doe, platform engineer.',
    ...overrides,
  };
}

/** The list shape: a saved CV without its text. */
function withoutText(cv: AdminSavedBaseCv): SavedBaseCv {
  return {
    id: cv.id,
    label: cv.label,
    original_filename: cv.original_filename,
    content_type: cv.content_type,
    size_bytes: cv.size_bytes,
    status: cv.status,
    character_count: cv.character_count,
    failure_reason: cv.failure_reason,
    failure_message: cv.failure_message,
    uploaded_at: cv.uploaded_at,
  };
}

export function posting(overrides: Partial<JobPosting> = {}): JobPosting {
  return {
    id: 'posting-1',
    source: 'pasted',
    source_url: null,
    title: 'Senior Platform Engineer',
    character_count: 900,
    text: 'We are hiring a platform engineer.',
    created_at: '2026-09-20T09:00:00Z',
    expires_at: null,
    ...overrides,
  };
}

export function summaryOf(p: JobPosting): JobPostingSummary {
  const { text, ...rest } = p;
  return { ...rest, preview: text.slice(0, 140) };
}

export function exportJob(overrides: Partial<ExportJob> = {}): ExportJob {
  return {
    id: 'job-1',
    tailoring_run_id: 'run-1',
    document: 'cv',
    format: 'pdf',
    layout_template: 'modern',
    status: 'ready',
    failure_reason: null,
    retryable: false,
    run_version: 3,
    current: true,
    byte_size: 84_000,
    render_duration_ms: 120,
    file_url: '/api/admin/users/u-ann/export-jobs/job-1/file',
    requested_at: '2026-09-20T10:01:00Z',
    started_at: '2026-09-20T10:01:01Z',
    completed_at: '2026-09-20T10:01:02Z',
    expires_at: null,
    ...overrides,
  };
}

export function succeededRun(overrides: Partial<TailoringRun> = {}): TailoringRun {
  return makeAccountRun({
    id: 'run-1',
    status: 'succeeded',
    base_cv_id: 'cv-1',
    job_posting_id: 'posting-1',
    tailored_cv: 'TAILORED CV BODY',
    cover_letter: 'COVER LETTER BODY',
    version: 3,
    ...overrides,
  });
}

export function entry(overrides: Partial<HistoryEntry> = {}): HistoryEntry {
  return makeHistoryEntry({ id: 'run-1', version: 3, ...overrides });
}

/** A history entry whose posting carries `title`. */
export function entryTitled(id: string, title: string): HistoryEntry {
  const base = entry({ id });
  return { ...base, posting: base.posting === null ? null : { ...base.posting, title } };
}

export function world(overrides: Partial<ContentWorld> = {}): ContentWorld {
  const p = posting();
  return {
    cvs: [savedCv()],
    postings: [p],
    historyPages: [[entry()]],
    runs: [succeededRun()],
    exports: { 'run-1': [exportJob()] },
    ...overrides,
  };
}

const gone = (code: string): Response =>
  jsonResponse(404, { error: { code, message: 'fixture message' } });

export const ERASURE: AdminHistoryEntryErasure = {
  tailoring_runs: 1,
  job_postings: 1,
  export_jobs: 1,
  tracked_applications: 0,
  files_unlinked: 1,
  unlink_failures: 0,
};

const P = '/api/admin/users/:id';

/**
 * The content routes plus what a signed-in admin's page needs around them. `overrides` (keyed
 * `'METHOD /path'`, the same key as below) replace a route; a replaced handler is still behind the
 * admin firewall.
 */
export function contentRoutes(
  w: ContentWorld,
  overrides: Readonly<Record<string, RouteHandler>> = {},
): Record<string, RouteHandler> {
  const guarded = (handler: RouteHandler): RouteHandler => adminOnly(ADMIN, handler);
  const defaults: Record<string, RouteHandler> = {
    [`GET ${P}`]: () => Response.json(TARGET),
    [`GET ${P}/base-cvs`]: () => Response.json({ items: w.cvs.map(withoutText) }),
    [`GET ${P}/base-cvs/:cvId`]: (call) => {
      const found = w.cvs.find((c) => call.path.endsWith(`/${c.id}`));
      return found ? Response.json(found) : gone('base_cv_not_found');
    },
    [`DELETE ${P}/base-cvs/:cvId`]: (call) => {
      const before = w.cvs.length;
      w.cvs = w.cvs.filter((c) => !call.path.endsWith(`/${c.id}`));
      return w.cvs.length === before
        ? gone('base_cv_not_found')
        : Response.json({ file_unlinked: true });
    },
    [`GET ${P}/job-postings`]: () => Response.json({ items: w.postings.map(summaryOf) }),
    [`GET ${P}/job-postings/:pid`]: (call) => {
      const found = w.postings.find((p) => call.path.endsWith(`/${p.id}`));
      return found ? Response.json(found) : gone('job_posting_not_found');
    },
    [`GET ${P}/tailoring-runs`]: (call) => {
      const cursor = call.query.get('cursor');
      const index = cursor === null ? 0 : Number(cursor.slice(1));
      const items = w.historyPages[index] ?? [];
      const next = index + 1 < w.historyPages.length ? `c${String(index + 1)}` : null;
      return Response.json({ items, next_cursor: next });
    },
    [`GET ${P}/tailoring-runs/:rid`]: (call) => {
      const found = w.runs.find((r) => call.path.endsWith(`/${r.id}`));
      return found ? Response.json(found) : gone('tailoring_run_not_found');
    },
    [`DELETE ${P}/tailoring-runs/:rid`]: (call) => {
      const found = w.runs.find((r) => call.path.endsWith(`/${r.id}`));
      if (!found) return gone('tailoring_run_not_found');
      w.runs = w.runs.filter((r) => r.id !== found.id);
      w.historyPages = w.historyPages.map((p) => p.filter((e) => e.id !== found.id));
      return Response.json(ERASURE);
    },
    [`GET ${P}/tailoring-runs/:rid/exports`]: (call) => {
      const id = call.path.split('/')[6] ?? '';
      return Response.json({ items: w.exports[id] ?? [] });
    },
    [`GET ${P}/tailoring-runs/:rid/documents/:kind/download`]: () =>
      new Response(new Blob(['document bytes']), { status: 200 }),
    [`GET ${P}/export-jobs/:jid/file`]: () =>
      new Response(new Blob(['file bytes']), { status: 200 }),
  };
  const merged = { ...defaults, ...overrides };
  return {
    ...signedInRoutes(ADMIN),
    ...GUEST_HOME,
    'GET /api/admin/access': noContent,
    'GET /api/admin/users': guarded(ok({ items: [TARGET], next_cursor: null })),
    ...Object.fromEntries(Object.entries(merged).map(([key, h]) => [key, guarded(h)])),
  };
}

export { userGone };
