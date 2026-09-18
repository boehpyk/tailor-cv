import { vi } from 'vitest';

/**
 * A `fetch` stub for the export feature's own endpoints — generalising `@/test/fixtures.ts`'s
 * `stubWorkspaceFetch` pattern to the four routes that module does not cover, exactly as
 * `features/editor/test/fetchStub.ts` did for the document `PUT`. Kept local for the same reason:
 * `qa` does not modify shared test infrastructure it did not write for this slice.
 *
 * Routes covered, all scoped to one run id (the export bar never reads another run's jobs):
 * - `GET /api/tailoring-runs/{runId}/exports` — the poller (`exportJobs`, call-numbered).
 * - `POST /api/tailoring-runs/{runId}/exports` — a request (`requestExport`).
 * - `GET /api/export-jobs/{jobId}/file` — a queued download, keyed by job id (`exportFile`).
 * - `GET /api/tailoring-runs/{runId}/documents/{kind}/download?format=...` — an inline download,
 *   keyed by `${kind}:${format}` (`downloadDocument`).
 *
 * An unhandled call rejects loudly rather than hanging, as `stubWorkspaceFetch` does.
 */
export interface ExportFetchStubs {
  readonly exportJobs?: (callNumber: number) => Promise<Response>;
  readonly requestExport?: (callNumber: number) => Promise<Response>;
  readonly exportFile?: Readonly<Record<string, (callNumber: number) => Promise<Response>>>;
  readonly downloadDocument?: Readonly<Record<string, (callNumber: number) => Promise<Response>>>;
}

export function stubExportFetch(runId: string, stubs: ExportFetchStubs): ReturnType<typeof vi.fn> {
  let exportJobsCalls = 0;
  let requestExportCalls = 0;
  const fileCallCounts = new Map<string, number>();
  const downloadCallCounts = new Map<string, number>();

  const fetchMock = vi.fn((input: string | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = init?.method ?? 'GET';
    const exportsPath = `/api/tailoring-runs/${runId}/exports`;

    if (url === exportsPath && method === 'GET') {
      if (stubs.exportJobs === undefined) {
        return Promise.reject(new Error(`unexpected GET ${url} in this test`));
      }
      exportJobsCalls += 1;
      return stubs.exportJobs(exportJobsCalls);
    }
    if (url === exportsPath && method === 'POST') {
      if (stubs.requestExport === undefined) {
        return Promise.reject(new Error(`unexpected POST ${url} in this test`));
      }
      requestExportCalls += 1;
      return stubs.requestExport(requestExportCalls);
    }

    const fileMatch = /^\/api\/export-jobs\/([^/]+)\/file$/.exec(url);
    if (fileMatch && method === 'GET') {
      const jobId = decodeURIComponent(fileMatch[1] ?? '');
      const handler = stubs.exportFile?.[jobId];
      if (handler === undefined) {
        return Promise.reject(new Error(`unexpected GET ${url} in this test`));
      }
      const callNumber = (fileCallCounts.get(jobId) ?? 0) + 1;
      fileCallCounts.set(jobId, callNumber);
      return handler(callNumber);
    }

    const downloadMatch =
      /^\/api\/tailoring-runs\/[^/]+\/documents\/([^/]+)\/download\?format=([^&]+)$/.exec(url);
    if (downloadMatch && method === 'GET') {
      const kind = decodeURIComponent(downloadMatch[1] ?? '');
      const format = decodeURIComponent(downloadMatch[2] ?? '');
      const key = `${kind}:${format}`;
      const handler = stubs.downloadDocument?.[key];
      if (handler === undefined) {
        return Promise.reject(new Error(`unexpected GET ${url} in this test`));
      }
      const callNumber = (downloadCallCounts.get(key) ?? 0) + 1;
      downloadCallCounts.set(key, callNumber);
      return handler(callNumber);
    }

    return Promise.reject(new Error(`unhandled fetch: ${method} ${url}`));
  });

  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** A successful bytes response, as `requestBlob` reads it — a body and a content type. */
export function blobResponse(
  status: number,
  contentType: string,
  bytes = 'fixture-bytes',
): Response {
  return new Response(new Blob([bytes], { type: contentType }), {
    status,
    headers: { 'Content-Type': contentType },
  });
}
