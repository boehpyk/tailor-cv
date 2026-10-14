import { ok, signedInRoutes } from './accountFetch';
import { ADMIN, exportJob, posting, savedCv, succeededRun } from './adminContentFetch';
import { GUEST_HOME, adminOnly, noContent } from './adminFetch';
import { jsonResponse, makeExtractedCv, makePostingSummary, makeRunSummary } from './fixtures';

import type { RouteHandler } from './accountFetch';
import type {
  AdminGuestSessionSummary,
  AdminStorage,
  GuestSessionErasure,
  PurgeReport,
  SweepReport,
} from '@/api/admin';
import type { BaseCv } from '@/features/intake/types';
import type { JobPosting, JobPostingSummary } from '@/features/posting/types';
import type { TailoringRun, TailoringRunSummary } from '@/features/tailoring/types';
import type { ExportJob } from '@/features/export/types';
import type { Readiness } from '@/api/health';

/**
 * Slice 4.4 test support (T21): a stateful fake of `/api/admin/guest-sessions…`, `/storage`,
 * `/guest-purge` and `/orphan-sweep`, plus `/health/ready`.
 *
 * **Per-bearer isolation** (2.4's `ed66fbe`): every admin route is behind `adminOnly(ADMIN, …)`, so
 * a request carrying anyone's credential but the admin's gets the firewall's code-less 404. A
 * session that is gone (or was deleted through the fake) answers 404 `guest_session_not_found` on
 * every route under it, like the server does.
 */

export { ADMIN };

export const SESSION_ID = 'g-1';
export const OTHER_SESSION_ID = 'g-2';

export function sessionSummary(
  overrides: Partial<AdminGuestSessionSummary> = {},
): AdminGuestSessionSummary {
  return {
    id: SESSION_ID,
    created_at: '2026-10-13T10:00:00Z',
    expires_at: '2026-10-14T10:00:00Z',
    // Four different numbers (none a date part), so a count wired to the wrong label fails.
    base_cvs: 3,
    job_postings: 4,
    tailoring_runs: 5,
    export_jobs: 6,
    ...overrides,
  };
}

export interface GuestContent {
  cvs: BaseCv[];
  postings: JobPostingSummary[];
  runs: TailoringRunSummary[];
}

export interface GuestWorld {
  /** The list, paged: page `i` is fetched with cursor `c{i}` (page 0 with none). */
  sessionPages: AdminGuestSessionSummary[][];
  content: Record<string, GuestContent>;
  /** Bodies served by the read pages (any session). */
  cvBody: string;
  postingBody: string;
  run: TailoringRun;
  exports: ExportJob[];
  /** Every route under a session answers 404 `guest_session_not_found`. */
  gone: boolean;
  storage: AdminStorage;
  purgePreview: PurgeReport;
  purgeRun: PurgeReport;
  sweepPreview: SweepReport;
  sweepRun: SweepReport;
  erasure: GuestSessionErasure;
  /** `null` serves 500 (the endpoint failing); otherwise the report with `status`. */
  readiness: Readiness | null;
  readinessStatus: number;
}

export const STORAGE: AdminStorage = {
  scanned_at: '2026-10-14T09:30:00Z',
  guest: { files: 12, bytes: 5_000_000_000 },
  user: { files: 31, bytes: 3_000_000 },
  unreferenced: { files: 7, bytes: 4_096 },
  partial: { files: 2, bytes: 1_024 },
  unrecognized: { files: 1, bytes: 512 },
};

export const PURGE_PREVIEW: PurgeReport = {
  examined: 3,
  sessions_deleted: 3,
  sessions_skipped: 0,
  sessions_failed: 0,
  files_unlinked: 7,
  files_failed: 0,
  overdue: 4,
  dry_run: true,
  lock: null,
};

export const PURGE_RUN: PurgeReport = {
  examined: 3,
  sessions_deleted: 3,
  sessions_skipped: 0,
  sessions_failed: 1,
  files_unlinked: 7,
  files_failed: 2,
  overdue: 9,
  dry_run: false,
  lock: 'acquired',
};

export const SWEEP_PREVIEW: SweepReport = {
  scanned: 20,
  referenced: 6,
  too_young: 2,
  unrecognized: 1,
  reclaimed: 5,
  failed: 0,
  dry_run: true,
};

export const SWEEP_RUN: SweepReport = { ...SWEEP_PREVIEW, failed: 0, dry_run: false };

export function readinessReport(overrides: Partial<Readiness> = {}): Readiness {
  return {
    ready: true,
    dependencies: {
      postgres: { healthy: true, detail: null },
      redis: { healthy: true, detail: null },
      celery: { healthy: true, detail: null },
    },
    jobs: {
      guest_purge: {
        scheduled: true,
        last_run: '2026-10-14T09:00:00Z',
        last_run_age_seconds: 600,
        last_outcome: 'ok',
        overdue: 4,
        stale: false,
        detail: null,
      },
      identity_token_sweep: { scheduled: true, overdue: 2, detail: null },
    },
    ...overrides,
  };
}

export function guestWorld(overrides: Partial<GuestWorld> = {}): GuestWorld {
  const run = succeededRun({ id: 'run-1', base_cv_id: 'cv-1', job_posting_id: 'posting-1' });
  const first: GuestContent = {
    cvs: [makeExtractedCv({ id: 'cv-1', original_filename: 'guest-cv.pdf' })],
    postings: [makePostingSummary({ id: 'posting-1', title: 'Guest-held posting' })],
    runs: [makeRunSummary({ id: 'run-1', status: 'succeeded', version: 3 })],
  };
  return {
    sessionPages: [[sessionSummary()]],
    content: {
      [SESSION_ID]: first,
      [OTHER_SESSION_ID]: { cvs: [], postings: [], runs: [] },
    },
    cvBody: 'Guest CV text.',
    postingBody: 'Guest posting text.',
    run,
    exports: [exportJob({ id: 'job-1', tailoring_run_id: 'run-1' })],
    gone: false,
    storage: STORAGE,
    purgePreview: PURGE_PREVIEW,
    purgeRun: PURGE_RUN,
    sweepPreview: SWEEP_PREVIEW,
    sweepRun: SWEEP_RUN,
    erasure: { files_unlinked: 3, unlink_failures: 0 },
    readiness: readinessReport(),
    readinessStatus: 200,
    ...overrides,
  };
}

export const sessionGone = (): Response =>
  jsonResponse(404, { error: { code: 'guest_session_not_found', message: 'fixture message' } });

const G = '/api/admin/guest-sessions/:id';

/** Everything an admin page needs around the guest and storage routes, keyed `'METHOD /path'`. */
export function guestRoutes(
  w: GuestWorld,
  overrides: Readonly<Record<string, RouteHandler>> = {},
): Record<string, RouteHandler> {
  const guarded = (handler: RouteHandler): RouteHandler => adminOnly(ADMIN, handler);
  const idOf = (path: string): string => path.split('/')[4] ?? '';
  const live = (id: string): AdminGuestSessionSummary | undefined =>
    w.gone ? undefined : w.sessionPages.flat().find((s) => s.id === id);
  /** The server's rule: any route under a session is 404 once the session is gone. */
  const underSession =
    (handler: RouteHandler): RouteHandler =>
    (call, n) =>
      live(idOf(call.path)) === undefined ? sessionGone() : handler(call, n);

  const defaults: Record<string, RouteHandler> = {
    'GET /api/admin/guest-sessions': (call) => {
      const cursor = call.query.get('cursor');
      const index = cursor === null ? 0 : Number(cursor.slice(1));
      const next = index + 1 < w.sessionPages.length ? `c${String(index + 1)}` : null;
      return Response.json({ items: w.sessionPages[index] ?? [], next_cursor: next });
    },
    [`GET ${G}`]: underSession((call) => {
      const id = idOf(call.path);
      const session = live(id);
      const held = w.content[id] ?? { cvs: [], postings: [], runs: [] };
      return Response.json({
        session,
        base_cvs: held.cvs,
        job_postings: held.postings,
        tailoring_runs: held.runs,
      });
    }),
    [`DELETE ${G}`]: underSession((call) => {
      const id = idOf(call.path);
      w.sessionPages = w.sessionPages.map((page) => page.filter((s) => s.id !== id));
      return Response.json(w.erasure);
    }),
    [`GET ${G}/base-cvs/:cvId`]: underSession(() =>
      Response.json(
        savedCv({
          id: 'cv-1',
          label: null,
          original_filename: 'guest-cv.pdf',
          extracted_text: w.cvBody,
        }),
      ),
    ),
    [`GET ${G}/job-postings/:pid`]: underSession(() =>
      Response.json(posting({ id: 'posting-1', text: w.postingBody }) satisfies JobPosting),
    ),
    [`GET ${G}/tailoring-runs/:rid`]: underSession(() => Response.json(w.run)),
    [`GET ${G}/tailoring-runs/:rid/exports`]: underSession(() =>
      Response.json({ items: w.exports }),
    ),
    [`GET ${G}/tailoring-runs/:rid/documents/:kind/download`]: underSession(
      () => new Response(new Blob(['document bytes']), { status: 200 }),
    ),
    [`GET ${G}/export-jobs/:jid/file`]: underSession(
      () => new Response(new Blob(['file bytes']), { status: 200 }),
    ),
    'GET /api/admin/storage': () => Response.json(w.storage),
    'GET /api/admin/guest-purge': () => Response.json(w.purgePreview),
    'POST /api/admin/guest-purge': () => Response.json(w.purgeRun),
    'GET /api/admin/orphan-sweep': () => Response.json(w.sweepPreview),
    'POST /api/admin/orphan-sweep': () => Response.json(w.sweepRun),
  };
  const merged = { ...defaults, ...overrides };
  return {
    ...signedInRoutes(ADMIN),
    ...GUEST_HOME,
    'GET /api/admin/access': noContent,
    'GET /api/admin/users': guarded(ok({ items: [], next_cursor: null })),
    'GET /health/ready': () =>
      w.readiness === null
        ? jsonResponse(500, { error: { code: 'internal_error', message: 'fixture message' } })
        : jsonResponse(w.readinessStatus, w.readiness),
    ...Object.fromEntries(Object.entries(merged).map(([key, h]) => [key, guarded(h)])),
  };
}
