import { act, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithQuery } from '@/test/render';

import { TailorPanel } from './TailorPanel';

import type { BaseCv } from '@/features/intake/types';
import type { JobPostingSummary } from '@/features/posting/types';
import type { TailoringFailureReason, TailoringRun, TailoringRunSummary } from '../types';

/**
 * T41 RED — the behavioural contract for `TailorPanel` (feature-spec.md's AC-13, AC-17, AC-25,
 * AC-28, AC-29, AC-30, AC-31 and the failure contract's "User sees" column; technical-plan.md's
 * "Frontend (`web/`)" section, in particular the four-states-plus-three-error-kinds table).
 *
 * Written against the **spec**, not against `TailorPanel.tsx`'s T40 skeleton — the skeleton calls
 * all five hooks but reads none of them and renders one fixed placeholder
 * (`<p>Placeholder (T40 skeleton).</p>`), so every test below is expected to fail **on its
 * assertion** until T42. Where a test would pass against the inert skeleton for the wrong reason,
 * that is a vacuity trap named in the T41 task-list entry (2026-09-11) and is handled explicitly at
 * the point it matters, never left to look like an accident:
 *
 * - **Trap 1** — the panel takes no props and reads `useBaseCvs` / `useJobPostings` /
 *   `useTailoringRuns` itself, so every test below stubs all three list endpoints.
 * - **Trap 2** — the skeleton passes `null` to `useTailoringRun`, so "no further request after a
 *   terminal status / a 4xx" would pass against it trivially. Every polling test first asserts the
 *   run **was** fetched at least once before asserting that polling stopped.
 * - **Trap 3** — "no failure language", "no retry control" and "C's text is not B's" would all pass
 *   against an empty stub. Every such negative assertion below follows a positive assertion that the
 *   state's own copy is genuinely on screen.
 */

const GEMINI_SENTENCE =
  'Tailoring sends your CV text and this job posting to Google Gemini. Nothing else is sent.';

/**
 * The retention sentence must be **copied verbatim from the existing intake/posting panels**
 * (task-list T41), not invented fresh for this one. Either of the two counts — this panel is not
 * asked to add a third variant about tailoring runs specifically.
 */
const RETENTION_SENTENCE_CV = 'We delete guest CVs after 24 hours.';
const RETENTION_SENTENCE_POSTING = 'We delete guest job postings after 24 hours.';

const BASE_CV_ID = '0192f0a1-aaaa-7000-8000-00000000aaaa';
const JOB_POSTING_ID = '0192f0a1-bbbb-7000-8000-00000000bbbb';

/** Exact copy from the failure contract's "User sees" column — the client maps `code`/`failure_reason` to these, never relays the server's `message` verbatim. */
const COPY = {
  tooManyTailoringRuns: "You've reached the limit for this session.", // G-10
  rateLimited: /too many tailoring runs/i, // G-11 (the "N minutes" tail is server-computed, not pinned here)
  rateLimitUnavailable: 'Tailoring is temporarily unavailable.', // G-12
  alreadyRunning: 'You already have a tailoring run in progress.', // G-9
  llmUnavailable: "We couldn't reach the model.", // G-16
  llmRateLimited: 'The model is busy right now.', // G-17
  llmRefused: 'The model declined to rewrite this content.', // G-18
  llmTimedOut: 'That took too long.', // G-19
  llmOutputInvalid: "The model's answer wasn't usable.", // G-20/G-21
  inputsTooLarge: 'Your CV is longer than we can tailor in one go — trim it and upload again.', // G-22
} as const;

/** AC-13's exact six reasons — the four retried-once classes plus the two never retried. */
const FAILURE_REASON_CASES: ReadonlyArray<{
  reason: TailoringFailureReason;
  retryable: boolean;
  copy: string;
}> = [
  { reason: 'llm_unavailable', retryable: true, copy: COPY.llmUnavailable },
  { reason: 'llm_rate_limited', retryable: true, copy: COPY.llmRateLimited },
  { reason: 'llm_timed_out', retryable: true, copy: COPY.llmTimedOut },
  { reason: 'llm_output_invalid', retryable: true, copy: COPY.llmOutputInvalid },
  { reason: 'llm_refused', retryable: false, copy: COPY.llmRefused },
  { reason: 'inputs_too_large', retryable: false, copy: COPY.inputsTooLarge },
];

function jsonResponse(status: number, body: unknown, headers?: Record<string, string>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

function makeExtractedCv(overrides: Partial<BaseCv> = {}): BaseCv {
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

function makePostingSummary(overrides: Partial<JobPostingSummary> = {}): JobPostingSummary {
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

function makeRunSummary(overrides: Partial<TailoringRunSummary> = {}): TailoringRunSummary {
  return {
    id: '0192f0a1-cccc-7000-8000-00000000cccc',
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
    ...overrides,
  };
}

function makeRun(overrides: Partial<TailoringRun> = {}): TailoringRun {
  return {
    id: '0192f0a1-cccc-7000-8000-00000000cccc',
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
    ...overrides,
  };
}

interface Stubs {
  readonly baseCvs?: () => Promise<Response>;
  readonly jobPostings?: () => Promise<Response>;
  readonly runsList?: () => Promise<Response>;
  readonly createRun?: () => Promise<Response>;
  /** Keyed by run id. Each handler receives the 1-based call number for THAT id. */
  readonly runDetail?: Record<string, (callNumber: number) => Promise<Response>>;
}

/**
 * A `fetch` stub that routes on URL and method — necessary here (unlike the sibling panels'
 * method-only routing) because `TailorPanel` reads three list endpoints itself (trap 1) plus a
 * dynamic per-run detail endpoint while polling.
 */
function stubFetch(stubs: Stubs): ReturnType<typeof vi.fn> {
  const detailCallCounts = new Map<string, number>();
  // Narrowed to `string | URL` rather than the full `RequestInfo | URL` — this codebase's
  // `client.ts` always calls `fetch` with a string path, and `URL` (unlike `Request`) has a safe,
  // meaningful `toString()`.
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
    const detailMatch = /^\/api\/tailoring-runs\/(.+)$/.exec(url);
    if (detailMatch) {
      const id = decodeURIComponent(detailMatch[1]);
      const handler = stubs.runDetail?.[id];
      if (handler === undefined) {
        return Promise.reject(new Error(`unexpected GET ${url} in this test`));
      }
      const callNumber = (detailCallCounts.get(id) ?? 0) + 1;
      detailCallCounts.set(id, callNumber);
      return handler(callNumber);
    }
    return Promise.reject(new Error(`unhandled fetch: ${method} ${url}`));
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/**
 * Count calls to `path` by HTTP method (default `GET`, since that's what every list/detail read in
 * this suite is). **This must stay method-aware**: `/api/tailoring-runs` is both the list `GET`
 * (which trap 1 requires this panel to issue on every render) and the create `POST`, so counting the
 * URL alone cannot tell "no request was issued" apart from "the list was loaded as usual".
 */
function countCallsTo(
  fetchMock: ReturnType<typeof vi.fn>,
  path: string,
  method: 'GET' | 'POST' = 'GET',
): number {
  return fetchMock.mock.calls.filter(([input, init]) => {
    const url = typeof input === 'string' ? input : String(input);
    const callMethod = (init as RequestInit | undefined)?.method ?? 'GET';
    return url === path && callMethod === method;
  }).length;
}

/** Flush pending microtasks under fake timers without waiting on a real clock. */
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

describe('TailorPanel', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  // ---------------------------------------------------------------------------------------------
  // 1. Loading
  // ---------------------------------------------------------------------------------------------

  it('shows the loading state and no button while the list queries are pending', () => {
    stubFetch({
      baseCvs: () => new Promise<Response>(() => undefined),
      jobPostings: () => new Promise<Response>(() => undefined),
      runsList: () => new Promise<Response>(() => undefined),
    });

    renderWithQuery(<TailorPanel />);

    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------------------------
  // 2. Empty
  // ---------------------------------------------------------------------------------------------

  it('AC-25: states the Gemini disclosure sentence and the retention promise once settled with no runs', async () => {
    stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });

    renderWithQuery(<TailorPanel />);

    expect(await screen.findByText(GEMINI_SENTENCE)).toBeInTheDocument();
    const retentionSentence =
      screen.queryByText(RETENTION_SENTENCE_CV) ?? screen.queryByText(RETENTION_SENTENCE_POSTING);
    expect(retentionSentence).toBeInTheDocument();
  });

  it('disables the launch control with a stated (accessible) reason when both inputs are missing', async () => {
    stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [] })),
      runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });

    renderWithQuery(<TailorPanel />);
    await screen.findByText(GEMINI_SENTENCE);

    const button = screen.getByRole('button');
    expect(button).toBeDisabled();
    // "Stated reason" means announced, not merely visually greyed out — a screen-reader user must
    // be told *why*, which is what an accessible description is for.
    expect(button).toHaveAccessibleDescription();
  });

  // ---------------------------------------------------------------------------------------------
  // 3. Working — both sub-states
  // ---------------------------------------------------------------------------------------------

  it('AC-29: a queued run renders "Waiting for a worker…"', async () => {
    const runId = 'run-queued';
    stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () =>
        Promise.resolve(
          jsonResponse(200, { items: [makeRunSummary({ id: runId, status: 'queued' })] }),
        ),
      runDetail: {
        [runId]: () => Promise.resolve(jsonResponse(200, makeRun({ id: runId, status: 'queued' }))),
      },
    });

    renderWithQuery(<TailorPanel />);

    expect(await screen.findByText('Waiting for a worker…')).toBeInTheDocument();
    expect(screen.queryByText(/Tailoring with Gemini/)).not.toBeInTheDocument();
  });

  it('AC-29: a running run renders "Tailoring with Gemini…" with a live elapsed count that ticks', async () => {
    vi.useFakeTimers();
    const runId = 'run-running';
    stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () =>
        Promise.resolve(
          jsonResponse(200, { items: [makeRunSummary({ id: runId, status: 'running' })] }),
        ),
      runDetail: {
        [runId]: () =>
          Promise.resolve(jsonResponse(200, makeRun({ id: runId, status: 'running' }))),
      },
    });

    renderWithQuery(<TailorPanel />);
    await flushMicrotasks();

    const readElapsedSeconds = (): number => {
      const node = screen.getByText(/Tailoring with Gemini/);
      const text = node.textContent;
      const match = /(\d+)\s*s\b/.exec(text);
      if (match === null) {
        throw new Error(`no elapsed seconds count found in "${text}"`);
      }
      return Number(match[1]);
    };

    const before = readElapsedSeconds();
    await advance(3000);
    const after = readElapsedSeconds();

    expect(after).toBeGreaterThan(before);
  });

  it('AC-29: after 20s of running, shows the "taking longer than usual" message', async () => {
    vi.useFakeTimers();
    const runId = 'run-running-slow';
    stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () =>
        Promise.resolve(
          jsonResponse(200, { items: [makeRunSummary({ id: runId, status: 'running' })] }),
        ),
      runDetail: {
        [runId]: () =>
          Promise.resolve(jsonResponse(200, makeRun({ id: runId, status: 'running' }))),
      },
    });

    renderWithQuery(<TailorPanel />);
    await flushMicrotasks();

    expect(
      screen.queryByText("This is taking longer than usual — we are still working. Don't refresh."),
    ).not.toBeInTheDocument();

    await advance(21_000);

    expect(
      screen.getByText("This is taking longer than usual — we are still working. Don't refresh."),
    ).toBeInTheDocument();
  });

  it('AC-28: the working state contains none of the failure copy and offers no "Try again" control', async () => {
    const runId = 'run-running-clean';
    stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () =>
        Promise.resolve(
          jsonResponse(200, { items: [makeRunSummary({ id: runId, status: 'running' })] }),
        ),
      runDetail: {
        [runId]: () =>
          Promise.resolve(jsonResponse(200, makeRun({ id: runId, status: 'running' }))),
      },
    });

    renderWithQuery(<TailorPanel />);

    // Positive half (trap 3): the working state's own copy really is on screen.
    expect(await screen.findByText(/Tailoring with Gemini/)).toBeInTheDocument();

    // Negative half: none of the failure copy, and no retry control.
    for (const { copy } of FAILURE_REASON_CASES) {
      expect(screen.queryByText(copy)).not.toBeInTheDocument();
    }
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------------------------
  // 4. Success
  // ---------------------------------------------------------------------------------------------

  it('AC-31: renders both documents with their character counts, and a <script> body as visible text with no innerHTML set from it', async () => {
    const runId = 'run-succeeded';
    const maliciousCv = 'Jane Doe <script>alert(1)</script> Senior Engineer';
    const coverLetter = 'Dear Hiring Manager, I am writing to apply for this role.';
    const scriptsBefore = document.querySelectorAll('script').length;
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => undefined);

    stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeRunSummary({
                id: runId,
                status: 'succeeded',
                tailored_cv_character_count: 777,
                cover_letter_character_count: 888,
              }),
            ],
          }),
        ),
      runDetail: {
        [runId]: () =>
          Promise.resolve(
            jsonResponse(
              200,
              makeRun({
                id: runId,
                status: 'succeeded',
                tailored_cv: maliciousCv,
                cover_letter: coverLetter,
                tailored_cv_character_count: 777,
                cover_letter_character_count: 888,
                model: 'gemini-fake',
                prompt_version: 'v1',
                llm_duration_ms: 1200,
                started_at: '2026-09-12T10:00:01Z',
                completed_at: '2026-09-12T10:00:05Z',
              }),
            ),
          ),
      },
    });

    renderWithQuery(<TailorPanel />);

    expect(await screen.findByText(maliciousCv)).toBeInTheDocument();
    expect(screen.getByText(coverLetter)).toBeInTheDocument();
    expect(screen.getByText(/777/)).toBeInTheDocument();
    expect(screen.getByText(/888/)).toBeInTheDocument();

    // AC-31: rendered as text, not markup. No new <script> element was created from the model's
    // output, and any accidental execution path (dangerouslySetInnerHTML would run it) is proven
    // absent by the alert spy never firing.
    expect(document.querySelectorAll('script').length).toBe(scriptsBefore);
    expect(alertSpy).not.toHaveBeenCalled();
  });

  // ---------------------------------------------------------------------------------------------
  // 5. Error A — rejected before the request
  // ---------------------------------------------------------------------------------------------

  it('error A: no base CV — the control is disabled with a stated reason and no request is issued', async () => {
    const fetchMock = stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });
    const user = userEvent.setup();

    renderWithQuery(<TailorPanel />);
    await screen.findByText(GEMINI_SENTENCE);

    const button = screen.getByRole('button');
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription();

    await user.click(button);

    expect(countCallsTo(fetchMock, '/api/tailoring-runs', 'POST')).toBe(0);
  });

  it('error A: no job posting — the control is disabled with a stated reason and no request is issued', async () => {
    const fetchMock = stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [] })),
      runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });
    const user = userEvent.setup();

    renderWithQuery(<TailorPanel />);
    await screen.findByText(GEMINI_SENTENCE);

    const button = screen.getByRole('button');
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription();

    await user.click(button);

    expect(countCallsTo(fetchMock, '/api/tailoring-runs', 'POST')).toBe(0);
  });

  it('error A: the newest base CV is not yet extracted — the control is disabled and no request is issued', async () => {
    const fetchMock = stubFetch({
      baseCvs: () =>
        Promise.resolve(jsonResponse(200, { items: [makeExtractedCv({ status: 'uploaded' })] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });
    const user = userEvent.setup();

    renderWithQuery(<TailorPanel />);
    await screen.findByText(GEMINI_SENTENCE);

    const button = screen.getByRole('button');
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription();

    await user.click(button);

    expect(countCallsTo(fetchMock, '/api/tailoring-runs', 'POST')).toBe(0);
  });

  // ---------------------------------------------------------------------------------------------
  // 6. Error B — rejected by the API, keyed off `code`
  // ---------------------------------------------------------------------------------------------

  it('error B: 409 too_many_tailoring_runs shows the code-mapped copy, never the server message', async () => {
    const junkMessage = 'ZZZZ_SERVER_JUNK_NOT_FOR_USERS';
    stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
      createRun: () =>
        Promise.resolve(
          jsonResponse(409, { error: { code: 'too_many_tailoring_runs', message: junkMessage } }),
        ),
    });
    const user = userEvent.setup();

    renderWithQuery(<TailorPanel />);
    await screen.findByText(GEMINI_SENTENCE);
    await user.click(screen.getByRole('button'));

    expect(await screen.findByText(COPY.tooManyTailoringRuns)).toBeInTheDocument();
    expect(screen.queryByText(junkMessage)).not.toBeInTheDocument();
  });

  it('error B: 429 rate_limited shows the code-mapped copy, never the server message', async () => {
    const junkMessage = 'ZZZZ_SERVER_JUNK_NOT_FOR_USERS';
    stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
      createRun: () =>
        Promise.resolve(
          jsonResponse(
            429,
            { error: { code: 'rate_limited', message: junkMessage } },
            { 'Retry-After': '600' },
          ),
        ),
    });
    const user = userEvent.setup();

    renderWithQuery(<TailorPanel />);
    await screen.findByText(GEMINI_SENTENCE);
    await user.click(screen.getByRole('button'));

    expect(await screen.findByText(COPY.rateLimited)).toBeInTheDocument();
    expect(screen.queryByText(junkMessage)).not.toBeInTheDocument();
  });

  it('error B: 503 rate_limit_unavailable shows the code-mapped copy, never the server message', async () => {
    const junkMessage = 'ZZZZ_SERVER_JUNK_NOT_FOR_USERS';
    stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
      createRun: () =>
        Promise.resolve(
          jsonResponse(503, { error: { code: 'rate_limit_unavailable', message: junkMessage } }),
        ),
    });
    const user = userEvent.setup();

    renderWithQuery(<TailorPanel />);
    await screen.findByText(GEMINI_SENTENCE);
    await user.click(screen.getByRole('button'));

    expect(await screen.findByText(COPY.rateLimitUnavailable)).toBeInTheDocument();
    expect(screen.queryByText(junkMessage)).not.toBeInTheDocument();
  });

  it('decision (a): a 409 tailoring_already_running offers "View the run in progress" and attaches to the id in the error body, not the list\'s newest item', async () => {
    const oldRunId = 'run-old-succeeded';
    const activeRunId = 'run-active-elsewhere';
    expect(activeRunId).not.toBe(oldRunId);

    const fetchMock = stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      // The list's newest run is a DIFFERENT id from the one the 409 names — a component that
      // (wrongly) re-derives "the run to watch" from the list instead of from the error body would
      // attach to this one and this test would catch it.
      runsList: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeRunSummary({
                id: oldRunId,
                status: 'succeeded',
                tailored_cv_character_count: 10,
                cover_letter_character_count: 10,
              }),
            ],
          }),
        ),
      createRun: () =>
        Promise.resolve(
          jsonResponse(409, {
            error: {
              code: 'tailoring_already_running',
              message: 'irrelevant server prose',
              active_tailoring_run_id: activeRunId,
            },
          }),
        ),
      runDetail: {
        [oldRunId]: () =>
          Promise.resolve(
            jsonResponse(
              200,
              makeRun({
                id: oldRunId,
                status: 'succeeded',
                tailored_cv: 'old tailored cv',
                cover_letter: 'old cover letter',
                tailored_cv_character_count: 10,
                cover_letter_character_count: 10,
              }),
            ),
          ),
        [activeRunId]: () =>
          Promise.resolve(jsonResponse(200, makeRun({ id: activeRunId, status: 'running' }))),
      },
    });
    const user = userEvent.setup();

    renderWithQuery(<TailorPanel />);
    // Confirm the success state (from the OLD run) rendered first.
    await screen.findByText('old tailored cv');

    await user.click(screen.getByRole('button', { name: /tailor again/i }));

    expect(await screen.findByText(COPY.alreadyRunning)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /view the run in progress/i }));

    // Attached to the ACTIVE run, proven by its own distinct copy — the old run never renders
    // "Tailoring with Gemini…", only a run whose status is 'running' does.
    expect(await screen.findByText(/Tailoring with Gemini/)).toBeInTheDocument();
    expect(countCallsTo(fetchMock, `/api/tailoring-runs/${activeRunId}`)).toBeGreaterThanOrEqual(1);
  });

  // ---------------------------------------------------------------------------------------------
  // 7. Error C — the run itself failed, keyed off `failure_reason`
  // ---------------------------------------------------------------------------------------------

  it.each(FAILURE_REASON_CASES)(
    'error C: failure_reason=$reason shows its own copy and Try again is retryable=$retryable',
    async ({ reason, retryable, copy }) => {
      const runId = `run-failed-${reason}`;
      stubFetch({
        baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
        jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
        runsList: () =>
          Promise.resolve(
            jsonResponse(200, {
              items: [
                makeRunSummary({ id: runId, status: 'failed', failure_reason: reason, retryable }),
              ],
            }),
          ),
        runDetail: {
          [runId]: () =>
            Promise.resolve(
              jsonResponse(
                200,
                makeRun({ id: runId, status: 'failed', failure_reason: reason, retryable }),
              ),
            ),
        },
      });

      renderWithQuery(<TailorPanel />);

      // Positive half (trap 3): this reason's own copy really is on screen.
      expect(await screen.findByText(copy)).toBeInTheDocument();

      if (retryable) {
        expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
      } else {
        expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument();
      }
    },
  );

  it("error C's text is not error B's", async () => {
    // Error B first, to capture its rendered text for comparison.
    const junkMessage = 'ZZZZ_SERVER_JUNK_NOT_FOR_USERS';
    stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
      createRun: () =>
        Promise.resolve(
          jsonResponse(409, { error: { code: 'too_many_tailoring_runs', message: junkMessage } }),
        ),
    });
    const user = userEvent.setup();
    const renderB = renderWithQuery(<TailorPanel />);
    await screen.findByText(GEMINI_SENTENCE);
    await user.click(screen.getByRole('button'));
    const textB = (await screen.findByText(COPY.tooManyTailoringRuns)).textContent;
    renderB.unmount();
    vi.unstubAllGlobals();

    // Error C — the run itself failed (llm_refused, chosen because it is one of the two with no
    // retry control, keeping this test's assertions unambiguous about which button is expected).
    vi.stubGlobal('fetch', vi.fn());
    const runId = 'run-failed-for-b-vs-c';
    stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeRunSummary({
                id: runId,
                status: 'failed',
                failure_reason: 'llm_refused',
                retryable: false,
              }),
            ],
          }),
        ),
      runDetail: {
        [runId]: () =>
          Promise.resolve(
            jsonResponse(
              200,
              makeRun({
                id: runId,
                status: 'failed',
                failure_reason: 'llm_refused',
                retryable: false,
              }),
            ),
          ),
      },
    });
    renderWithQuery(<TailorPanel />);
    const textC = (await screen.findByText(COPY.llmRefused)).textContent;

    expect(textC).not.toBe(textB);
  });

  // ---------------------------------------------------------------------------------------------
  // 8. Polling stops on a terminal status (AC-30) — trap 2: assert it WAS fetched first
  // ---------------------------------------------------------------------------------------------

  it('AC-30: polling stops once a run goes from running to succeeded', async () => {
    vi.useFakeTimers();
    const runId = 'run-becomes-succeeded';
    const detailPath = `/api/tailoring-runs/${runId}`;
    const fetchMock = stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () =>
        Promise.resolve(
          jsonResponse(200, { items: [makeRunSummary({ id: runId, status: 'running' })] }),
        ),
      runDetail: {
        [runId]: (callNumber) =>
          Promise.resolve(
            jsonResponse(
              200,
              callNumber === 1
                ? makeRun({ id: runId, status: 'running' })
                : makeRun({
                    id: runId,
                    status: 'succeeded',
                    tailored_cv: 'final cv',
                    cover_letter: 'final letter',
                    tailored_cv_character_count: 8,
                    cover_letter_character_count: 12,
                  }),
            ),
          ),
      },
    });

    renderWithQuery(<TailorPanel />);
    await flushMicrotasks();

    // Trap 2: prove the run WAS fetched (and the working state rendered) before claiming polling
    // ever stopped — this is what an inert `useTailoringRun(null)` could never satisfy.
    expect(screen.getByText(/Tailoring with Gemini/)).toBeInTheDocument();
    expect(countCallsTo(fetchMock, detailPath)).toBeGreaterThanOrEqual(1);

    // RTL's `findByText` cannot be used here: its `asyncWrapper` only advances fake timers when a
    // global `jest` exists (`jestFakeTimersAreEnabled` in
    // `node_modules/@testing-library/react/dist/pure.js`), which Vitest never defines, so it would
    // wait on a real 5000ms timeout instead of the faked clock. A deterministic advance replaces it:
    // the first 1000ms tick (`POLL_INTERVAL_MS` in `useTailoringRun.ts`) is what sends the terminal
    // request, and — observed empirically — the response's `.then` chain (fetch's body read, JSON
    // parse, the query's state update and React's re-render) does not finish draining inside that
    // same `act(async () => vi.advanceTimersByTimeAsync(...))` call; it settles during the *next*
    // tick's microtask flush instead, with no further request issued in between (`callsAtTerminal`
    // below stays at the count reached after the first tick). So two ticks, not one, is the smallest
    // reliable advance.
    await advance(1000);
    await advance(1000);
    expect(screen.getByText('final cv')).toBeInTheDocument();
    const callsAtTerminal = countCallsTo(fetchMock, detailPath);

    await advance(10_000);
    expect(countCallsTo(fetchMock, detailPath)).toBe(callsAtTerminal);
  });

  it('AC-30: polling stops once a run goes from running to failed', async () => {
    vi.useFakeTimers();
    const runId = 'run-becomes-failed';
    const detailPath = `/api/tailoring-runs/${runId}`;
    const fetchMock = stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () =>
        Promise.resolve(
          jsonResponse(200, { items: [makeRunSummary({ id: runId, status: 'running' })] }),
        ),
      runDetail: {
        [runId]: (callNumber) =>
          Promise.resolve(
            jsonResponse(
              200,
              callNumber === 1
                ? makeRun({ id: runId, status: 'running' })
                : makeRun({
                    id: runId,
                    status: 'failed',
                    failure_reason: 'llm_timed_out',
                    retryable: true,
                  }),
            ),
          ),
      },
    });

    renderWithQuery(<TailorPanel />);
    await flushMicrotasks();

    expect(screen.getByText(/Tailoring with Gemini/)).toBeInTheDocument();
    expect(countCallsTo(fetchMock, detailPath)).toBeGreaterThanOrEqual(1);

    // See the sibling "…to succeeded" test above for why `getByText` after two deterministic
    // `advance(1000)` ticks replaces `findByText` here (RTL's fake-timer detection never fires under
    // Vitest, so `findByText` would wait on a real 5000ms timeout instead of the faked clock; the
    // first tick sends the terminal request, the second is where its response finishes draining into
    // the DOM, with no further request issued in between).
    await advance(1000);
    await advance(1000);
    expect(screen.getByText(COPY.llmTimedOut)).toBeInTheDocument();
    const callsAtTerminal = countCallsTo(fetchMock, detailPath);

    await advance(10_000);
    expect(countCallsTo(fetchMock, detailPath)).toBe(callsAtTerminal);
  });

  // ---------------------------------------------------------------------------------------------
  // 9. Decision (b): polling stops on a 4xx mid-poll too, not only on a terminal status
  // ---------------------------------------------------------------------------------------------

  it('decision (b): polling stops when a run 404s mid-poll (purged)', async () => {
    vi.useFakeTimers();
    const runId = 'run-purged-mid-poll';
    const detailPath = `/api/tailoring-runs/${runId}`;
    const fetchMock = stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () =>
        Promise.resolve(
          jsonResponse(200, { items: [makeRunSummary({ id: runId, status: 'running' })] }),
        ),
      runDetail: {
        [runId]: (callNumber) =>
          Promise.resolve(
            callNumber === 1
              ? jsonResponse(200, makeRun({ id: runId, status: 'running' }))
              : jsonResponse(404, {
                  error: { code: 'tailoring_run_not_found', message: "We couldn't find that run." },
                }),
          ),
      },
    });

    renderWithQuery(<TailorPanel />);
    await flushMicrotasks();

    // Trap 2 again: the run was genuinely being watched before the 404 arrived.
    expect(screen.getByText(/Tailoring with Gemini/)).toBeInTheDocument();
    expect(countCallsTo(fetchMock, detailPath)).toBeGreaterThanOrEqual(1);

    await advance(1000); // the 404 lands
    const callsAfter404 = countCallsTo(fetchMock, detailPath);
    expect(callsAfter404).toBeGreaterThanOrEqual(2);

    await advance(10_000); // must not keep polling a run that is gone
    expect(countCallsTo(fetchMock, detailPath)).toBe(callsAfter404);
  });

  it('decision (b): polling stops when a run 401s mid-poll (session expired)', async () => {
    vi.useFakeTimers();
    const runId = 'run-session-expires-mid-poll';
    const detailPath = `/api/tailoring-runs/${runId}`;
    const fetchMock = stubFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () =>
        Promise.resolve(
          jsonResponse(200, { items: [makeRunSummary({ id: runId, status: 'running' })] }),
        ),
      runDetail: {
        [runId]: (callNumber) =>
          Promise.resolve(
            callNumber === 1
              ? jsonResponse(200, makeRun({ id: runId, status: 'running' }))
              : jsonResponse(401, {
                  error: { code: 'guest_session_expired', message: 'Your session has expired.' },
                }),
          ),
      },
    });

    renderWithQuery(<TailorPanel />);
    await flushMicrotasks();

    expect(screen.getByText(/Tailoring with Gemini/)).toBeInTheDocument();
    expect(countCallsTo(fetchMock, detailPath)).toBeGreaterThanOrEqual(1);

    await advance(1000); // the 401 lands
    const callsAfter401 = countCallsTo(fetchMock, detailPath);
    expect(callsAfter401).toBeGreaterThanOrEqual(2);

    await advance(10_000);
    expect(countCallsTo(fetchMock, detailPath)).toBe(callsAfter401);
  });
});
