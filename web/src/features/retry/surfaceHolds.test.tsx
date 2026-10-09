import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { BaseCvUploadPanel } from '@/features/intake/components/BaseCvUploadPanel';
import { JobPostingPanel } from '@/features/posting/components/JobPostingPanel';
import { SavedCvUploadControl } from '@/features/savedCvs/components/SavedCvUploadControl';
import { USER_A, signInAs } from '@/test/accountFetch';
import { jsonResponse, makePostingSummary } from '@/test/fixtures';
import { renderWithQuery } from '@/test/render';

import { ExportBar } from '../export/components/ExportBar';
import { stubExportFetch } from '../export/test/fetchStub';
import { EXPORT_RUN_ID } from '../export/test/fixtures';

import type { ReactNode } from 'react';

/**
 * T16 RED (slice 3.3; AC-11, AC-12, AC-13) — a refusal holds the control that caused it, and only it.
 *
 * Time base (T14's lesson): every flush is zero-time, so a deadline is exactly (the instant the
 * click was made) + the window, and "deadline - 1 ms" is computed from the clock as it reads —
 * `advanceToward` — never a fixed `N - 1` after a helper that moved the clock. Typing and clicking
 * use `userEvent` with `delay: null`, which schedules no timers.
 *
 * Traps honoured: a hold is `toBeDisabled()` **and** no request; a release is `toBeEnabled()`;
 * each absence has a positive control (the control worked before, a sibling still works during).
 */

// Verify's MINOR 1: once a hold is released, no sentence may still name the wait.
const WAIT_NAMED = /try again (?:at \d|in \d+ seconds?)/i;

const NOW = 1_700_000_000_000;
const WINDOW_MS = 120_000;
// 120 s is beyond 90 s, so the phrase is a clock time in the browser's locale (jsdom: en-US,
// "10:16 PM"); AC-5 forbids forcing one, so the shape accepts both.
const CLOCK_TIME = /\d{1,2}:\d{2}(?:\s?[AP]M)?/;
const SERVER_PROSE = 'Too many requests. Try again in 120 seconds.';

function tooMany(code = 'rate_limited'): Response {
  return jsonResponse(429, { error: { code, message: SERVER_PROSE } }, { 'Retry-After': '120' });
}

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

async function advanceToward(deadlineMs: number, offsetMs: number): Promise<void> {
  await advance(deadlineMs + offsetMs - Date.now());
}

function postsTo(fetchMock: ReturnType<typeof vi.fn>, path: string): number {
  return fetchMock.mock.calls.filter((call) => {
    const [input, init] = call as [unknown, RequestInit | undefined];
    return String(input) === path && init?.method === 'POST';
  }).length;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  __resetForTests();
});

// --- AC-11: export ----------------------------------------------------------------------------------

describe('AC-11 — a refused PDF request holds the PDF control only', () => {
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

  const button = (name: string) => screen.getByRole('button', { name });
  // Scoped to the PDF control's region: a refused Word request shows its own *Export again* (X-22).
  const exportAgain = () =>
    within(button('PDF').parentElement as HTMLElement).getByRole('button', {
      name: /export again/i,
    });

  it('disables PDF, sends nothing on click, keeps Word usable, and releases at the deadline', async () => {
    const fetchMock = stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () => Promise.resolve(jsonResponse(200, { items: [] })),
      requestExport: (n) =>
        Promise.resolve(
          n === 1
            ? tooMany()
            : jsonResponse(503, { error: { code: 'service_unavailable', message: 'x' } }),
        ),
    });
    renderBar();
    await flush();
    expect(button('PDF')).toBeEnabled(); // positive control: it was usable before the 429

    const clickedAt = Date.now();
    fireEvent.click(button('PDF'));
    await flush();

    expect(postsTo(fetchMock, `/api/tailoring-runs/${EXPORT_RUN_ID}/exports`)).toBe(1);
    // The PDF control's region: its button and the "Export again" action beside the message.
    const region = button('PDF').parentElement as HTMLElement;
    expect(within(region).getByText(CLOCK_TIME)).toBeInTheDocument();
    for (const control of within(region).getAllByRole('button')) {
      expect(control).toBeDisabled();
    }
    fireEvent.click(exportAgain());
    await flush();
    expect(postsTo(fetchMock, `/api/tailoring-runs/${EXPORT_RUN_ID}/exports`)).toBe(1);

    // The other formats are untouched: Word is still sendable (a second POST goes out).
    expect(button('Markdown')).toBeEnabled();
    expect(button('Plain text')).toBeEnabled();
    expect(button('Word')).toBeEnabled();
    fireEvent.click(button('Word'));
    await flush();
    expect(postsTo(fetchMock, `/api/tailoring-runs/${EXPORT_RUN_ID}/exports`)).toBe(2);

    await advanceToward(clickedAt + WINDOW_MS, -1);
    expect(exportAgain()).toBeDisabled();
    await advanceToward(clickedAt + WINDOW_MS, 0);
    expect(exportAgain()).toBeEnabled();
    expect(screen.getByText('You can try again now.').closest('[role="status"]')).not.toBeNull();
    expect(screen.queryAllByText(WAIT_NAMED)).toHaveLength(0);

    await advance(5000); // deadline + 5 s: nothing is re-sent on the user's behalf (AC-22)
    expect(postsTo(fetchMock, `/api/tailoring-runs/${EXPORT_RUN_ID}/exports`)).toBe(2);
  });

  it('a 503 on the same control holds nothing', async () => {
    stubExportFetch(EXPORT_RUN_ID, {
      exportJobs: () => Promise.resolve(jsonResponse(200, { items: [] })),
      requestExport: () =>
        Promise.resolve(
          jsonResponse(503, { error: { code: 'service_unavailable', message: 'x' } }),
        ),
    });
    renderBar();
    await flush();

    fireEvent.click(button('PDF'));
    await flush();

    expect(exportAgain()).toBeEnabled(); // the refusal landed, and nothing holds its retry
  });
});

// --- AC-12: uploads ---------------------------------------------------------------------------------

const CV_FILE = () => new File(['cv'], 'cv.pdf', { type: 'application/pdf' });

function fileInput(): HTMLInputElement {
  const input = document.querySelector('input[type="file"]');
  if (!(input instanceof HTMLInputElement)) {
    throw new Error('no file input rendered');
  }
  return input;
}

describe('AC-12 — guest upload (BaseCvUploadPanel)', () => {
  function stub(): ReturnType<typeof vi.fn> {
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
      Promise.resolve(init?.method === 'POST' ? tooMany() : jsonResponse(200, { items: [] })),
    );
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('a 429 disables the dropzone input, names the time, sends nothing, and releases', async () => {
    const fetchMock = stub();
    renderWithQuery(<BaseCvUploadPanel />);
    await flush();
    expect(fileInput()).toBeEnabled(); // positive control

    const clickedAt = Date.now();
    fireEvent.change(fileInput(), { target: { files: [CV_FILE()] } });
    await flush();

    expect(postsTo(fetchMock, '/api/base-cvs')).toBe(1);
    expect(fileInput()).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent(CLOCK_TIME);
    expect(document.body).not.toHaveTextContent('Try again in');
    fireEvent.click(fileInput());
    await flush();
    expect(postsTo(fetchMock, '/api/base-cvs')).toBe(1);

    await advanceToward(clickedAt + WINDOW_MS, -1);
    expect(fileInput()).toBeDisabled();
    await advanceToward(clickedAt + WINDOW_MS, 0);
    expect(fileInput()).toBeEnabled();
    expect(screen.getByText('You can try again now.').closest('[role="status"]')).not.toBeNull();
    expect(screen.queryAllByText(WAIT_NAMED)).toHaveLength(0);
    await advance(5000);
    expect(postsTo(fetchMock, '/api/base-cvs')).toBe(1);
  });

  it('a 503 holds nothing', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
        Promise.resolve(
          init?.method === 'POST'
            ? jsonResponse(503, { error: { code: 'rate_limit_unavailable', message: 'x' } })
            : jsonResponse(200, { items: [] }),
        ),
      ),
    );
    renderWithQuery(<BaseCvUploadPanel />);
    await flush();

    fireEvent.change(fileInput(), { target: { files: [CV_FILE()] } });
    await flush();

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(fileInput()).toBeEnabled();
  });
});

describe('AC-12 — saved-CV upload (SavedCvUploadControl)', () => {
  beforeEach(() => {
    signInAs(USER_A);
  });

  it('a 429 disables the input, names the time, sends nothing, and releases', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(tooMany()));
    vi.stubGlobal('fetch', fetchMock);
    renderWithQuery(<SavedCvUploadControl notice="Kept until you delete it." />);
    await flush();
    expect(fileInput()).toBeEnabled();

    const clickedAt = Date.now();
    fireEvent.change(fileInput(), { target: { files: [CV_FILE()] } });
    await flush();

    expect(postsTo(fetchMock, '/api/me/base-cvs')).toBe(1);
    expect(fileInput()).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent(CLOCK_TIME);
    expect(document.body).not.toHaveTextContent('Try again in');
    fireEvent.click(fileInput());
    await flush();
    expect(postsTo(fetchMock, '/api/me/base-cvs')).toBe(1);

    await advanceToward(clickedAt + WINDOW_MS, -1);
    expect(fileInput()).toBeDisabled();
    await advanceToward(clickedAt + WINDOW_MS, 0);
    expect(fileInput()).toBeEnabled();
    expect(screen.getByText('You can try again now.').closest('[role="status"]')).not.toBeNull();
    expect(screen.queryAllByText(WAIT_NAMED)).toHaveLength(0);
    await advance(5000);
    expect(postsTo(fetchMock, '/api/me/base-cvs')).toBe(1);
  });
});

// --- AC-13: job posting -----------------------------------------------------------------------------

describe('AC-13 — job posting Add', () => {
  const PASTED = 'Senior Widget Engineer, remote. '.repeat(10);

  function stub(secondPost?: () => Response): ReturnType<typeof vi.fn> {
    let posts = 0;
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST') {
        posts += 1;
        return Promise.resolve(posts === 1 || secondPost === undefined ? tooMany() : secondPost());
      }
      return Promise.resolve(jsonResponse(200, { items: [] }));
    });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  function submit(): HTMLElement {
    return screen.getByRole('button', { name: /add job posting/i });
  }

  it('a 429 on a pasted posting holds submission, never shows the server prose, and releases', async () => {
    const fetchMock = stub();
    renderWithQuery(<JobPostingPanel />);
    await flush();
    fireEvent.change(screen.getByLabelText(/job posting text/i), { target: { value: PASTED } });
    expect(submit()).toBeEnabled(); // positive control

    const clickedAt = Date.now();
    fireEvent.click(submit());
    await flush();

    expect(postsTo(fetchMock, '/api/job-postings')).toBe(1);
    expect(submit()).toBeDisabled();
    expect(document.body).toHaveTextContent(CLOCK_TIME);
    // AC-13: error B is worded from the code; the server's sentence never reaches the DOM.
    expect(document.body).not.toHaveTextContent('Try again in');
    fireEvent.click(submit());
    await flush();
    expect(postsTo(fetchMock, '/api/job-postings')).toBe(1);

    await advanceToward(clickedAt + WINDOW_MS, -1);
    expect(submit()).toBeDisabled();
    await advanceToward(clickedAt + WINDOW_MS, 0);
    expect(submit()).toBeEnabled();
    expect(screen.queryAllByText(WAIT_NAMED)).toHaveLength(0);
    await advance(5000);
    expect(postsTo(fetchMock, '/api/job-postings')).toBe(1);
  });

  it('a 429 on a fetched posting holds Fetch, offers Paste instead, and a paste is sendable (F-5)', async () => {
    const fetchMock = stub(() => jsonResponse(201, makePostingSummary()));
    renderWithQuery(<JobPostingPanel />);
    await flush();
    fireEvent.click(screen.getByRole('radio', { name: /link to the posting/i }));
    fireEvent.change(screen.getByLabelText(/job posting url/i), {
      target: { value: 'https://jobs.example.com/p/1' },
    });
    expect(submit()).toBeEnabled(); // positive control

    fireEvent.click(submit());
    await flush();

    expect(postsTo(fetchMock, '/api/job-postings')).toBe(1);
    expect(submit()).toBeDisabled(); // Fetch is held
    expect(document.body).not.toHaveTextContent('Try again in');
    const pasteInstead = screen.getByRole('button', { name: /paste the description instead/i });
    expect(pasteInstead).toBeEnabled();

    fireEvent.click(pasteInstead);
    await flush();
    fireEvent.change(screen.getByLabelText(/job posting text/i), { target: { value: PASTED } });
    expect(submit()).toBeEnabled(); // only the refused source is held
    fireEvent.click(submit());
    await flush();
    expect(postsTo(fetchMock, '/api/job-postings')).toBe(2);
  });

  it('a 503 holds nothing', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
        Promise.resolve(
          init?.method === 'POST'
            ? jsonResponse(503, { error: { code: 'rate_limit_unavailable', message: 'x' } })
            : jsonResponse(200, { items: [] }),
        ),
      ),
    );
    renderWithQuery(<JobPostingPanel />);
    await flush();
    fireEvent.change(screen.getByLabelText(/job posting text/i), { target: { value: PASTED } });

    fireEvent.click(submit());
    await flush();

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(submit()).toBeEnabled();
  });
});
