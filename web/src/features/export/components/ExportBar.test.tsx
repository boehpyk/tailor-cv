import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { jsonResponse } from '@/test/fixtures';

import { exportJobsQueryKey } from '../hooks/useExportJobs';
import { blobResponse, stubExportFetch } from '../test/fetchStub';
import { EXPORT_RUN_ID, makeExportJob } from '../test/fixtures';
import { ExportBar } from './ExportBar';

import type { SaveState } from '@/features/editor/saveState';
import type { DocumentProblem } from '@/features/tailoring/types';
import type { ReactNode } from 'react';
import type { ExportBarProps } from './ExportBar';

/**
 * F5 RED — `ExportBar`'s loading / error / empty / success states (feature-spec AC-35, AC-36,
 * AC-37, AC-38, AC-39, AC-40, AC-41, AC-42, AC-43; technical-plan "Loading / error / empty /
 * success" → "The export bar").
 *
 * Written against the **spec**, not `ExportBar.tsx`'s F4 skeleton, which calls none of the hooks
 * this file drives through `fetch`, renders four static buttons with no `disabled` attribute, and
 * exactly **two** empty `role="status"` regions (`ExportControl.tsx`'s own docstring: one per
 * *queued* format, the pre-amendment reading of AC-36). Every assertion below fails on a real
 * mismatch — missing text, an element that is not disabled, two `role="status"` elements where
 * four are required — never on an `ImportError`, because every prop, hook and type this file
 * exercises already exists (F1–F4).
 *
 * Every expected string is copied from `feature-spec.md` / `technical-plan.md` verbatim, never
 * imported from `exportCopy.ts` — importing the constant under test would make the assertion
 * tautological the day that constant itself is wrong.
 *
 * AC-42's 409 `export_not_ready` row ("the control returns to its polled state") is exercised at
 * the pure-function layer only (`exportView.test.ts`), not here: reproducing the exact race that
 * produces it — a click landing after the job has already moved on — would mean contriving a DOM
 * state no real user path reaches, where the unit test states the contract directly.
 */

async function flushMicrotasks(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
}

const SAVED: SaveState = { kind: 'saved' };

interface RenderedBar {
  readonly queryClient: QueryClient;
  readonly rerender: (next: Partial<ExportBarProps>) => void;
  readonly unmount: () => void;
  readonly container: HTMLElement;
}

function renderBar(
  overrides: Partial<ExportBarProps> = {},
  queryClient?: QueryClient,
): RenderedBar {
  const client = queryClient ?? makeQueryClient();
  const props: ExportBarProps = {
    runId: EXPORT_RUN_ID,
    document: 'cv',
    saveState: SAVED,
    ...overrides,
  };

  function Wrapper({ children }: { children: ReactNode }): React.JSX.Element {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  }

  const { rerender, unmount, container } = render(<ExportBar {...props} />, { wrapper: Wrapper });

  return {
    queryClient: client,
    container,
    unmount,
    rerender: (next: Partial<ExportBarProps>) => {
      rerender(
        <Wrapper>
          <ExportBar {...props} {...next} />
        </Wrapper>,
      );
    },
  };
}

/** `URL.createObjectURL`/`revokeObjectURL` do not exist in jsdom (measured: jsdom 25 leaves both
 * `undefined`), so `saveBlob`'s two calls are stubbed by hand rather than spied on. */
function stubObjectUrl(): {
  readonly createObjectURL: ReturnType<typeof vi.fn>;
  readonly revokeObjectURL: ReturnType<typeof vi.fn>;
} {
  const createObjectURL = vi.fn(() => 'blob:mock-url');
  const revokeObjectURL = vi.fn();
  URL.createObjectURL = createObjectURL;
  URL.revokeObjectURL = revokeObjectURL;
  return { createObjectURL, revokeObjectURL };
}

/** Captures every anchor's `download` attribute at the moment it is clicked — `saveBlob` never
 * appends the anchor to the document, so it cannot be found by querying the DOM afterwards. */
function spyOnAnchorClicks(): { readonly downloads: string[] } {
  const downloads: string[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    downloads.push(this.download);
  });
  return { downloads };
}

const SAVE_STATE_GATE_CASES: ReadonlyArray<{ readonly name: string; readonly state: SaveState }> = [
  { name: 'dirty', state: { kind: 'dirty' } },
  { name: 'saving', state: { kind: 'saving' } },
  { name: 'failed', state: { kind: 'failed', retry: () => undefined } },
  {
    name: 'conflict',
    state: { kind: 'conflict', loadLatest: () => undefined, keepMine: () => undefined },
  },
  { name: 'paused', state: { kind: 'paused', retryAfterSeconds: 30 } },
  { name: 'invalid', state: { kind: 'invalid', problem: 'too_long' as DocumentProblem } },
  { name: 'expired', state: { kind: 'expired' } },
];

describe('ExportBar', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('AC-43 loading: four disabled controls and "Checking your downloads…"', () => {
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () => new Promise<Response>(() => undefined),
    });

    renderBar();

    expect(screen.getByText('Checking your downloads…')).toBeInTheDocument();
    for (const name of ['Markdown', 'Plain text', 'PDF', 'Word']) {
      expect(screen.getByRole('button', { name })).toBeDisabled();
    }
  });

  it('AC-43 error: shows "We couldn\'t check your downloads" with Check again, and keeps the two inline controls enabled', async () => {
    vi.useFakeTimers();
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () => Promise.reject(new TypeError('Failed to fetch')),
    });

    renderBar();
    await flushMicrotasks();
    await advance(1000); // retry #1 backoff
    await advance(2000); // retry #2 backoff
    await advance(4000); // retry #3 backoff — retries exhausted
    await advance(1000); // drain the settled error state into the DOM

    expect(screen.getByText(/we couldn't check your downloads/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /check again/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Markdown' })).not.toBeDisabled();
    expect(screen.getByRole('button', { name: 'Plain text' })).not.toBeDisabled();
  });

  it('AC-43 empty / AC-36 amendment: no jobs renders four enabled controls, each with its own live region (four, not two)', async () => {
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });

    renderBar();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'PDF' })).not.toBeDisabled();
    });
    for (const name of ['Markdown', 'Plain text', 'PDF', 'Word']) {
      expect(screen.getByRole('button', { name })).not.toBeDisabled();
    }
    expect(screen.getAllByRole('status')).toHaveLength(4);
  });

  it('AC-36: the four controls render in the order Markdown, Plain text, PDF, Word', async () => {
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });

    const { container } = renderBar();
    await screen.findByRole('button', { name: 'PDF' });

    const buttons = within(container).getAllByRole('button');
    expect(buttons.map((button) => button.textContent)).toEqual([
      'Markdown',
      'Plain text',
      'PDF',
      'Word',
    ]);
  });

  it('AC-37/AC-38: a queued PDF job shows "Waiting for a worker…" and offers no retry control', async () => {
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () =>
        Promise.resolve(
          jsonResponse(200, { items: [makeExportJob({ format: 'pdf', status: 'queued' })] }),
        ),
    });

    renderBar();

    expect(await screen.findByText('Waiting for a worker…')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /export again/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument();
  });

  it('AC-37: a rendering PDF job at 3 elapsed seconds shows "Preparing your PDF… 3s"', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-18T10:00:03.000Z'));
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeExportJob({
                format: 'pdf',
                status: 'rendering',
                requested_at: '2026-09-18T10:00:00.000Z',
                started_at: '2026-09-18T10:00:00.000Z',
              }),
            ],
          }),
        ),
    });

    renderBar();
    await flushMicrotasks();

    expect(screen.getByText('Preparing your PDF… 3s')).toBeInTheDocument();
  });

  it('AC-37: after 20s in queued/rendering the copy becomes the "taking longer than usual" notice', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-18T10:00:00.000Z'));
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeExportJob({
                format: 'pdf',
                status: 'queued',
                requested_at: '2026-09-18T10:00:00.000Z',
              }),
            ],
          }),
        ),
    });

    renderBar();
    await flushMicrotasks();
    await advance(20_000);

    expect(
      screen.getByText('This is taking longer than usual — we are still working.'),
    ).toBeInTheDocument();
  });

  it('AC-37: ready and not current is "stale" — "Your document changed — Export again"', async () => {
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeExportJob({ format: 'pdf', status: 'ready', current: false, byte_size: 1 }),
            ],
          }),
        ),
    });

    renderBar();

    expect(await screen.findByText('Your document changed — Export again')).toBeInTheDocument();
  });

  it('AC-37/AC-38: failed and retryable ("That took too long.") offers Export again; failed and NOT retryable does not', async () => {
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeExportJob({
                format: 'pdf',
                status: 'failed',
                failure_reason: 'render_timed_out',
                retryable: true,
              }),
            ],
          }),
        ),
    });
    const { unmount } = renderBar();

    expect(await screen.findByText('That took too long.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /export again/i })).toBeInTheDocument();
    unmount();

    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeExportJob({
                format: 'pdf',
                status: 'failed',
                failure_reason: 'render_failed',
                retryable: false,
              }),
            ],
          }),
        ),
    });
    renderBar();

    expect(
      await screen.findByText("We couldn't turn this document into a PDF. Edit it and try again."),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /export again/i })).not.toBeInTheDocument();
  });

  it('AC-39: polling stops once every job of the run is terminal', async () => {
    vi.useFakeTimers();
    const fetchMock = stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: (callNumber) =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeExportJob({
                format: 'pdf',
                status: callNumber === 1 ? 'queued' : 'ready',
                current: true,
                byte_size: 1,
              }),
            ],
          }),
        ),
    });

    renderBar();
    await flushMicrotasks();
    await advance(1000); // the poller's second fetch — the job is now `ready`, terminal
    await flushMicrotasks();

    const exportsPath = `/api/tailoring-runs/${EXPORT_RUN_ID}/exports`;
    const callsAfterReady = fetchMock.mock.calls.filter(
      (call) => (call[0] as string) === exportsPath,
    ).length;
    expect(callsAfterReady).toBe(2);

    await advance(3000); // no job is active any more — the third poll must not happen
    const callsLater = fetchMock.mock.calls.filter(
      (call) => (call[0] as string) === exportsPath,
    ).length;
    expect(callsLater).toBe(2);
  });

  for (const { name, state } of SAVE_STATE_GATE_CASES) {
    it(`AC-40: save state "${name}" disables all four controls`, async () => {
      stubExportFetch(EXPORT_RUN_ID, {
        exportJobs: () => Promise.resolve(jsonResponse(200, { items: [] })),
      });

      renderBar({ saveState: state });
      await screen.findByRole('button', { name: 'PDF' });

      for (const label of ['Markdown', 'Plain text', 'PDF', 'Word']) {
        expect(screen.getByRole('button', { name: label })).toBeDisabled();
      }
    });
  }

  it('AC-40: "saving" gives the exact reason "Save your changes first — Saving…"', async () => {
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });

    renderBar({ saveState: { kind: 'saving' } });

    expect(await screen.findByText('Save your changes first — Saving…')).toBeInTheDocument();
  });

  it('AC-40: "expired" gives the exact reason "Your session has expired" — no "save first" prefix', async () => {
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });

    renderBar({ saveState: { kind: 'expired' } });

    expect(await screen.findByText('Your session has expired')).toBeInTheDocument();
  });

  it('AC-40: the disabled reason renders once, beside the four controls, never inside one of their own live regions', async () => {
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });

    renderBar({ saveState: { kind: 'saving' } });

    // Exactly once: four copies of this sentence beside four disabled buttons would be the same
    // reason shouted four times, which is precisely what `ExportBar`'s docstring says the bar-level
    // placement (rather than a per-control one) is for.
    expect(await screen.findAllByText('Save your changes first — Saving…')).toHaveLength(1);

    // And it is not one of the four per-control status regions saying it: each of those stays
    // empty while the gate is the reason nothing is offered.
    for (const status of screen.getAllByRole('status')) {
      expect(status).not.toHaveTextContent('Save your changes first');
    }
  });

  it('AC-40: controls disable before the debounce fires (dirty), stay disabled while saving, and re-enable once saved', async () => {
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });

    const { rerender } = renderBar({ saveState: { kind: 'dirty' } });
    await screen.findByRole('button', { name: 'PDF' });
    expect(screen.getByRole('button', { name: 'PDF' })).toBeDisabled();

    rerender({ saveState: { kind: 'saving' } });
    expect(screen.getByRole('button', { name: 'PDF' })).toBeDisabled();

    rerender({ saveState: SAVED });
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'PDF' })).not.toBeDisabled();
    });
  });

  it("AC-41: a save's invalidation flips a ready control to stale, without a page reload", async () => {
    const readyJob = makeExportJob({
      id: 'job-ac41',
      format: 'pdf',
      status: 'ready',
      byte_size: 86016,
    });
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: (callNumber) =>
        Promise.resolve(jsonResponse(200, { items: [{ ...readyJob, current: callNumber === 1 }] })),
    });

    const { queryClient } = renderBar();
    await screen.findByText('Download PDF · 84 KB', { exact: false });

    // Simulates the one line `useDocumentAutosave`'s `onSuccess` already carries (F2, AC-41): a
    // successful save invalidates the run's export list on the SAME query client the bar reads.
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: exportJobsQueryKey(EXPORT_RUN_ID) });
    });

    expect(await screen.findByText('Your document changed — Export again')).toBeInTheDocument();
  });

  it('AC-42: 404 export_job_not_found maps to "We couldn\'t find that file"', async () => {
    const { createObjectURL } = stubObjectUrl();
    spyOnAnchorClicks();
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeExportJob({ id: 'job-404', format: 'pdf', status: 'ready', byte_size: 86016 }),
            ],
          }),
        ),
      exportFile: {
        'job-404': () =>
          Promise.resolve(
            jsonResponse(404, {
              error: { code: 'export_job_not_found', message: 'gone' },
            }),
          ),
      },
    });

    renderBar();
    await screen.findByText('Download PDF · 84 KB', { exact: false });
    fireEvent.click(screen.getByRole('button', { name: /pdf/i }));

    expect(await screen.findByText("We couldn't find that file")).toBeInTheDocument();
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('AC-42: 410 export_file_gone maps to "That file is no longer available — Export again", and the recovery the copy promises is reachable — MAJOR 2 (/verify slice 1.5)', async () => {
    stubObjectUrl();
    spyOnAnchorClicks();
    const exportsPath = `/api/tailoring-runs/${EXPORT_RUN_ID}/exports`;
    const fetchMock = stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeExportJob({ id: 'job-410', format: 'pdf', status: 'ready', byte_size: 86016 }),
            ],
          }),
        ),
      exportFile: {
        'job-410': () =>
          Promise.resolve(
            jsonResponse(410, { error: { code: 'export_file_gone', message: 'gone' } }),
          ),
      },
      // The retry this test drives must land here, on the exports resource, as a new request — not
      // on `exportFile['job-410']` again, which would just 410 a second time for the same reason.
      requestExport: () =>
        Promise.resolve(
          jsonResponse(
            202,
            makeExportJob({
              id: 'job-410-retry',
              format: 'pdf',
              status: 'queued',
              byte_size: null,
            }),
          ),
        ),
    });

    renderBar();
    await screen.findByText('Download PDF · 84 KB', { exact: false });
    fireEvent.click(screen.getByRole('button', { name: /pdf/i }));

    expect(
      await screen.findByText('That file is no longer available — Export again'),
    ).toBeInTheDocument();

    // Before MAJOR 2's fix, `primaryActionFor` decides a click's meaning from the job ROW, which a
    // 410 never changes — `job.status === 'ready' && job.current` is still true, so the button stays
    // wired to the identical `downloadExportFile('job-410')` call that just 410'd. Clicking "Try
    // again" must instead issue a NEW export request: X-47's stated recovery ("re-exporting does")
    // has to be something a click can actually do.
    fireEvent.click(screen.getByRole('button', { name: /try again/i }));

    await waitFor(() => {
      const postsToExports = fetchMock.mock.calls.filter(
        (call) =>
          (call[0] as string) === exportsPath &&
          ((call[1] as RequestInit | undefined)?.method ?? 'GET') === 'POST',
      );
      expect(postsToExports).toHaveLength(1);
    });

    // And the retry must NOT be a second GET on the file that already 410'd — that would be the bug
    // this test exists to catch, passing anyway because nobody checked what the click did.
    const fileCalls = fetchMock.mock.calls.filter(
      (call) => (call[0] as string) === '/api/export-jobs/job-410/file',
    );
    expect(fileCalls).toHaveLength(1);

    // The gap the reviewer flagged: `primaryActionFor`'s `downloadFailed` branch calls
    // `download.reset()` before `requestAgain()`. Without it, `downloadFailed` outranks the job row
    // in `viewOfExport` (it is a fact about this browser the server does not know yet), so the 410's
    // sentence would keep showing over a job that is actively rendering — a smaller instance of the
    // very defect MAJOR 2 fixed, introduced by its own fix. Observed failing with `download.reset()`
    // removed: the 410 sentence never left the screen (see the RED commit body for the exact output).
    expect(
      screen.queryByText('That file is no longer available — Export again'),
    ).not.toBeInTheDocument();
  });

  it('AC-42: 401 guest_session_expired maps to the session-expired copy', async () => {
    stubObjectUrl();
    spyOnAnchorClicks();
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeExportJob({ id: 'job-401', format: 'pdf', status: 'ready', byte_size: 86016 }),
            ],
          }),
        ),
      exportFile: {
        'job-401': () =>
          Promise.resolve(
            jsonResponse(401, { error: { code: 'guest_session_expired', message: 'expired' } }),
          ),
      },
    });

    renderBar();
    await screen.findByText('Download PDF · 84 KB', { exact: false });
    fireEvent.click(screen.getByRole('button', { name: /pdf/i }));

    expect(await screen.findByText(/session has expired/i)).toBeInTheDocument();
  });

  it('AC-42: a 5xx / network failure maps to "Couldn\'t download" with Try again', async () => {
    stubObjectUrl();
    spyOnAnchorClicks();
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeExportJob({ id: 'job-503', format: 'pdf', status: 'ready', byte_size: 86016 }),
            ],
          }),
        ),
      exportFile: {
        'job-503': () =>
          Promise.resolve(
            jsonResponse(503, { error: { code: 'service_unavailable', message: 'down' } }),
          ),
      },
    });

    renderBar();
    await screen.findByText('Download PDF · 84 KB', { exact: false });
    fireEvent.click(screen.getByRole('button', { name: /pdf/i }));

    expect(await screen.findByText("Couldn't download")).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });

  it('AC-42: a successful download calls createObjectURL and revokeObjectURL, and saves under the server\'s filename — never an <a href="/api/…">', async () => {
    const { createObjectURL, revokeObjectURL } = stubObjectUrl();
    const { downloads } = spyOnAnchorClicks();
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeExportJob({
                id: 'job-ok',
                document: 'cv',
                format: 'pdf',
                status: 'ready',
                current: true,
                byte_size: 86016,
              }),
            ],
          }),
        ),
      exportFile: {
        'job-ok': () => Promise.resolve(blobResponse(200, 'application/pdf')),
      },
    });

    const { container } = renderBar();
    await screen.findByText('Download PDF · 84 KB', { exact: false });
    fireEvent.click(screen.getByRole('button', { name: /pdf/i }));

    await waitFor(() => {
      expect(createObjectURL).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(revokeObjectURL).toHaveBeenCalled();
    });
    expect(downloads).toContain('tailored-cv.pdf');
    expect(container.querySelectorAll('a[href^="/api/"]')).toHaveLength(0);
  });

  it('AC-35: states where the files are made, how long they live, and that nothing else sees them', async () => {
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });

    renderBar();

    expect(
      await screen.findByText(
        'Your files are made on our server, kept for 24 hours, and never sent anywhere else.',
      ),
    ).toBeInTheDocument();
  });

  it("AC-36: switching :document switches which job's state the controls show", async () => {
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeExportJob({
                document: 'cv',
                format: 'pdf',
                status: 'ready',
                current: true,
                byte_size: 86016,
              }),
            ],
          }),
        ),
    });

    const { rerender } = renderBar({ document: 'cv' });

    expect(await screen.findByText('Download PDF · 84 KB', { exact: false })).toBeInTheDocument();

    rerender({ document: 'cover_letter' });

    await waitFor(() => {
      expect(screen.queryByText('Download PDF · 84 KB', { exact: false })).not.toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: 'PDF' })).not.toBeDisabled();
  });
});
