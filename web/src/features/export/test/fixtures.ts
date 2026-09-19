import type { ExportMutations } from '../exportView';
import type { ExportJob } from '../types';

/**
 * Fixture factories for the export feature's own tests — `exportView.test.ts` and
 * `ExportBar.test.tsx` (F5).
 *
 * Kept local to `features/export/test/`, not added to `@/test/fixtures.ts`, for the reason
 * `features/editor/test/fetchStub.ts`'s docstring gives: `qa` does not modify shared test
 * infrastructure it did not write for this slice. Wire fixtures only, `Partial<ExportJob>`
 * overrides, matching every sibling feature's fixture shape.
 */

export const EXPORT_RUN_ID = 'export-bar-fixture-run';

/**
 * One `ExportJob`, defaulted to a freshly `queued` PDF for the CV at run version 1 — every field
 * a real response carries, including the two the wire makes nullable on every status
 * (`byte_size`, `failure_reason`; see `exportView.ts`'s docstring on `ready.byteSize` and
 * `failed.reason`).
 */
export function makeExportJob(overrides: Partial<ExportJob> = {}): ExportJob {
  return {
    id: 'export-job-fixture',
    tailoring_run_id: EXPORT_RUN_ID,
    document: 'cv',
    format: 'pdf',
    status: 'queued',
    failure_reason: null,
    retryable: false,
    run_version: 1,
    current: true,
    byte_size: null,
    render_duration_ms: null,
    file_url: null,
    requested_at: '2026-09-18T10:00:00Z',
    started_at: null,
    completed_at: null,
    expires_at: '2026-09-19T10:00:00Z',
    ...overrides,
  };
}

/** `ExportMutations` with nothing in flight — the default for a table row that is not about a
 * request or a download. */
export function makeExportMutations(overrides: Partial<ExportMutations> = {}): ExportMutations {
  return {
    requesting: null,
    downloading: null,
    downloadFailure: null,
    requestFailure: null,
    ...overrides,
  };
}
