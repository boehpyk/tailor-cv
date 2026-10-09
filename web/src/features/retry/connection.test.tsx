import { QueryClient, QueryClientProvider, onlineManager } from '@tanstack/react-query';
import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { jsonResponse, makeRun, stubWorkspaceFetch } from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

import { ExportBar } from '../export/components/ExportBar';
import { stubExportFetch } from '../export/test/fetchStub';
import { EXPORT_RUN_ID, makeExportJob } from '../export/test/fixtures';

import type { ReactNode } from 'react';

/**
 * T20 RED (slice 3.3; AC-17, AC-18, AC-19, AC-20) — a poller says what it is doing while TanStack
 * retries or is paused, in words distinct from "lost contact".
 *
 * Fake timers, driven only by `advanceTimersByTimeAsync` (the sync form never settles TanStack's
 * promises and hangs); every test carries a timeout so a hang fails instead of stalling. The
 * retry delays are TanStack's real 1 s / 2 s / 4 s. `onlineManager` is a global, restored in
 * `afterEach`. Expected sentences are written out verbatim from the spec, not imported from
 * `retryCopy.ts`. Every absence has a positive control: the page was showing the working run, and
 * the line it is absent from was seen in the step before.
 */

const NOW = 1_700_000_000_000;
const TIMEOUT = 20_000;
const RUN_ID = 'run-page-fixture';
const TRYING = (attempt: number, subject: 'run' | 'file') =>
  `Connection trouble — trying again (attempt ${String(attempt)} of 4). ${
    subject === 'run' ? 'Your run is still working.' : 'Your file is still being prepared.'
  }`;
const OFFLINE_RUN =
  "You're offline. Your run keeps going on our side — we'll check again when you're back.";
const OFFLINE_FILE =
  "You're offline. Your file is still being prepared on our side — we'll check again when you're back.";
const LOST_CONTACT = 'We lost contact with your tailoring run. It may still be working.';
const NO_CONNECTION_LINE = /Connection trouble|You're offline/;

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function flush(): Promise<void> {
  for (let i = 0; i < 8; i += 1) {
    await advance(0);
  }
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  onlineManager.setOnline(true);
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// --- RunPage -----------------------------------------------------------------------------------------

type Poll = (callNumber: number) => Promise<Response>;

const runningOk = () =>
  Promise.resolve(jsonResponse(200, makeRun({ id: RUN_ID, status: 'running' })));
const networkDown = () => Promise.reject(new TypeError('Failed to fetch'));

function watchRun(poll: Poll): ReturnType<typeof stubWorkspaceFetch> {
  return stubWorkspaceFetch({ runDetail: { [RUN_ID]: poll } });
}

function posts(fetchMock: ReturnType<typeof vi.fn>): number {
  return fetchMock.mock.calls.filter((call) => {
    const [, init] = call as [unknown, RequestInit | undefined];
    return init?.method === 'POST';
  }).length;
}

async function openRunPage(): Promise<void> {
  renderWithRouter(`/runs/${RUN_ID}/cv`);
  await flush();
  expect(screen.getByText(/Tailoring with Gemini/)).toBeInTheDocument(); // the working run
}

describe('AC-17 — RunPage while the poll is failing', () => {
  it(
    'a network failure says it is trying again, the run still shows working, and the line goes after a success',
    async () => {
      const fetchMock = watchRun((n) => (n === 2 ? networkDown() : runningOk()));
      await openRunPage();
      expect(screen.queryByText(NO_CONNECTION_LINE)).not.toBeInTheDocument(); // control: quiet while healthy

      await advance(1000); // the poll fires and fails
      await flush();

      const line = screen.getByText(TRYING(2, 'run'));
      expect(line.closest('[role="status"]')).not.toBeNull();
      expect(screen.getByText(/Tailoring with Gemini/)).toBeInTheDocument();

      await advance(1000); // TanStack's first retry, 1 s later, succeeds
      await flush();

      expect(screen.queryByText(NO_CONNECTION_LINE)).not.toBeInTheDocument();
      expect(screen.getByText(/Tailoring with Gemini/)).toBeInTheDocument();
      expect(posts(fetchMock)).toBe(0);
    },
    TIMEOUT,
  );
});

describe('AC-18 — RunPage offline', () => {
  it(
    'says it will check again when back, hides the elapsed counter, and resumes when online',
    async () => {
      let polls = 0;
      const fetchMock = watchRun(() => {
        polls += 1;
        return runningOk();
      });
      await openRunPage();
      expect(screen.getByText(/^\d+s$/)).toBeInTheDocument(); // control: the counter was shown

      act(() => {
        onlineManager.setOnline(false);
      });
      await advance(1000); // the next poll is attempted offline, so TanStack pauses it
      await flush();

      expect(screen.getByText(OFFLINE_RUN).closest('[role="status"]')).not.toBeNull();
      expect(screen.queryByText(/^\d+s$/)).not.toBeInTheDocument();
      const pollsWhileOffline = polls;

      act(() => {
        onlineManager.setOnline(true);
      });
      await advance(1000);
      await flush();

      expect(polls).toBeGreaterThan(pollsWhileOffline);
      expect(screen.queryByText(NO_CONNECTION_LINE)).not.toBeInTheDocument();
      expect(posts(fetchMock)).toBe(0);
    },
    TIMEOUT,
  );
});

describe('AC-19 — one message at a time', () => {
  it(
    'four consecutive failures end in lost contact alone; the retry line is gone, no POST',
    async () => {
      const fetchMock = watchRun((n) => (n === 1 ? runningOk() : networkDown()));
      await openRunPage();

      await advance(1000);
      await flush();
      expect(screen.getByText(TRYING(2, 'run'))).toBeInTheDocument();
      await advance(1000);
      await flush();
      expect(screen.getByText(TRYING(3, 'run'))).toBeInTheDocument();
      await advance(2000);
      await flush();
      expect(screen.getByText(TRYING(4, 'run'))).toBeInTheDocument(); // control: seen until the end

      await advance(4000); // retries exhausted
      await flush();

      expect(screen.getByText(LOST_CONTACT)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /check again/i })).toBeInTheDocument();
      expect(screen.queryByText(NO_CONNECTION_LINE)).not.toBeInTheDocument();
      expect(posts(fetchMock)).toBe(0);
    },
    TIMEOUT,
  );

  it.each([
    ['404', 404, { code: 'tailoring_run_not_found', message: 'gone' }],
    ['401 guest_session_expired', 401, { code: 'guest_session_expired', message: 'expired' }],
  ])(
    'a %s shows no reconnecting line at all, and is not retried',
    async (_name, status, error) => {
      let polls = 0;
      watchRun((n) => {
        polls = n;
        return n === 1 ? runningOk() : Promise.resolve(jsonResponse(status, { error }));
      });
      await openRunPage();

      await advance(1000);
      await flush();
      await advance(10_000); // long enough for any retry backoff to have fired

      // Control: the refusal really was polled, once, and a 4xx is not treated as transient —
      // TanStack made no retry, so there is nothing to say "trying again" about.
      expect(polls).toBe(2);
      expect(screen.queryByText(NO_CONNECTION_LINE)).not.toBeInTheDocument();
    },
    TIMEOUT,
  );
});

// --- ExportBar ---------------------------------------------------------------------------------------

describe('AC-20 — the export bar, a PDF job rendering', () => {
  const renderingJob = makeExportJob({
    format: 'pdf',
    status: 'rendering',
    requested_at: new Date(NOW).toISOString().replace('.000Z', 'Z'),
    started_at: new Date(NOW).toISOString().replace('.000Z', 'Z'),
  });

  function renderBar(): void {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
    });
    function Wrapper({ children }: { children: ReactNode }): React.JSX.Element {
      return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    }
    render(
      <ExportBar
        runId={EXPORT_RUN_ID}
        document="cv"
        saveState={{ kind: 'saved' }}
        layout={null}
        onLayoutChange={() => undefined}
      />,
      { wrapper: Wrapper },
    );
  }

  const list = () => Promise.resolve(jsonResponse(200, { items: [renderingJob] }));

  it(
    'a failing poll says it is trying again in export words, and the line goes after a success',
    async () => {
      stubExportFetch(EXPORT_RUN_ID, { exportJobs: (n) => (n === 2 ? networkDown() : list()) });
      renderBar();
      await flush();
      expect(screen.getByText(/Preparing your PDF/)).toBeInTheDocument(); // control: rendering
      expect(screen.queryByText(NO_CONNECTION_LINE)).not.toBeInTheDocument();

      await advance(1000);
      await flush();

      expect(screen.getByText(TRYING(2, 'file')).closest('[role="status"]')).not.toBeNull();

      await advance(1000);
      await flush();

      expect(screen.queryByText(NO_CONNECTION_LINE)).not.toBeInTheDocument();
    },
    TIMEOUT,
  );

  it(
    'offline says the file is still being prepared, and resumes when back',
    async () => {
      let polls = 0;
      stubExportFetch(EXPORT_RUN_ID, {
        exportJobs: () => {
          polls += 1;
          return list();
        },
      });
      renderBar();
      await flush();
      expect(screen.getByText(/Preparing your PDF/)).toBeInTheDocument();

      act(() => {
        onlineManager.setOnline(false);
      });
      await advance(1000);
      await flush();

      expect(screen.getByText(OFFLINE_FILE).closest('[role="status"]')).not.toBeNull();
      const pollsWhileOffline = polls;

      act(() => {
        onlineManager.setOnline(true);
      });
      await advance(1000);
      await flush();

      expect(polls).toBeGreaterThan(pollsWhileOffline);
      expect(screen.queryByText(NO_CONNECTION_LINE)).not.toBeInTheDocument();
    },
    TIMEOUT,
  );
});
