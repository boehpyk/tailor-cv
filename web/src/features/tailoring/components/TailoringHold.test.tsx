import { act, fireEvent, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import {
  USER_A,
  callsTo,
  ok,
  signInAs,
  signedInRoutes,
  stubAccountFetch,
} from '@/test/accountFetch';
import {
  countCallsTo,
  jsonResponse,
  makeExtractedCv,
  makePostingSummary,
  makeRun,
  makeRunSummary,
  stubWorkspaceFetch,
} from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

/**
 * T14 RED (slice 3.3; AC-9, AC-10, AC-22) — a refusal holds the control that caused it.
 *
 * Fake timers from the first line, as `RunPage.test.tsx` does for its backoff test: a hold is a
 * claim about *when*, and a real clock cannot be asked to wait 120 s. Everything is driven with
 * `advanceTimersByTimeAsync`; the sync form never settles TanStack's promises.
 *
 * Traps honoured: a hold is proved by `toBeDisabled()` **and** an unchanged request count (a
 * disabled-looking control can still be wired); a release is proved by `toBeEnabled()` (a control
 * that was disabled all along satisfies `findByRole`); every absence has a positive control (the
 * control was enabled before the 429, and a 503 on the same control is a re-clickable button).
 */

// Verify's MINOR 1: once a hold is released, no sentence may still name the wait.
const WAIT_NAMED = /try again (?:at \d|in \d+ seconds?)/i;

const NOW = 1_700_000_000_000; // 22:13:20 UTC, a whole second
const SECOND = 1000;

const BASE_CV = makeExtractedCv();
const POSTING = makePostingSummary();

function tooMany(retryAfter: number): Response {
  return jsonResponse(
    429,
    { error: { code: 'rate_limited', message: 'Too many tailoring runs.' } },
    { 'Retry-After': String(retryAfter) },
  );
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** Load, requests and renders settle without a real clock. */
async function settle(stepMs = 0): Promise<void> {
  // Zero-time by default: flush promises and 0 ms timers without moving the clock, so a deadline
  // is exactly (receipt + window) and "deadline - 1 ms" really is 1 ms early. Only the account
  // workspace's boot needs real fake-time steps to load; nothing is measured against the clock
  // until after it has.
  for (let i = 0; i < 8; i += 1) {
    await advance(stepMs);
  }
}

/** Advance to `offsetMs` from `deadlineMs`, whatever the clock now reads. */
async function advanceToward(deadlineMs: number, offsetMs: number): Promise<void> {
  await advance(deadlineMs + offsetMs - Date.now());
}

function launchButton(): HTMLElement {
  return screen.getByRole('button', { name: /tailor/i });
}

function iso(ms: number): string {
  return new Date(ms).toISOString();
}

function isoWhole(ms: number): string {
  return iso(ms).replace('.000Z', 'Z');
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

// --- AC-9: TailorLaunch, guest workspace ------------------------------------------------------------

function stubGuestWorkspace(createRun: () => Promise<Response>) {
  return stubWorkspaceFetch({
    baseCvs: () => Promise.resolve(jsonResponse(200, { items: [BASE_CV] })),
    jobPostings: () => Promise.resolve(jsonResponse(200, { items: [POSTING] })),
    runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
    createRun,
  });
}

describe('AC-9 — Tailor my CV in the guest workspace', () => {
  beforeEach(() => {
    __resetForTests();
    authStore.signOut('expired');
  });

  it('a 429 with Retry-After: 120 disables it, sends nothing on click, and releases at +120 s', async () => {
    const fetchMock = stubGuestWorkspace(() => Promise.resolve(tooMany(120)));
    renderWithRouter('/');
    await settle();
    expect(launchButton()).toBeEnabled(); // positive control: it was usable before the 429

    fireEvent.click(launchButton());
    await settle();

    expect(countCallsTo(fetchMock, '/api/tailoring-runs', 'POST')).toBe(1);
    expect(launchButton()).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent(/\d{2}:\d{2}/); // names when it returns
    fireEvent.click(launchButton());
    await settle();
    expect(countCallsTo(fetchMock, '/api/tailoring-runs', 'POST')).toBe(1);

    const focused = document.activeElement;
    await advance(119_999);
    expect(launchButton()).toBeDisabled();
    await advance(1);
    expect(launchButton()).toBeEnabled();
    expect(screen.getByText('You can try again now.').closest('[role="status"]')).not.toBeNull();
    expect(screen.queryAllByText(WAIT_NAMED)).toHaveLength(0);
    expect(document.activeElement).toBe(focused);

    await advance(5000); // deadline + 5 s: nothing is sent on the user's behalf (AC-22)
    expect(countCallsTo(fetchMock, '/api/tailoring-runs', 'POST')).toBe(1);
  });

  it('a 503 holds nothing: the same control can be pressed again', async () => {
    const fetchMock = stubGuestWorkspace(() =>
      Promise.resolve(
        jsonResponse(503, { error: { code: 'rate_limit_unavailable', message: 'Unavailable.' } }),
      ),
    );
    renderWithRouter('/');
    await settle();

    fireEvent.click(launchButton());
    await settle();

    expect(screen.getByRole('alert')).toBeInTheDocument(); // positive control: the refusal landed
    expect(launchButton()).toBeEnabled();
    fireEvent.click(launchButton());
    await settle();
    expect(countCallsTo(fetchMock, '/api/tailoring-runs', 'POST')).toBe(2);
  });
});

// --- AC-9: TailorLaunch, account workspace ----------------------------------------------------------

describe('AC-9 — Tailor in the account workspace', () => {
  const SAVED_CV = {
    id: 'saved-cv-1',
    label: null,
    original_filename: 'jane-resume.pdf',
    content_type: 'application/pdf',
    size_bytes: 2048,
    status: 'extracted',
    character_count: 512,
    failure_reason: null,
    failure_message: null,
    uploaded_at: '2026-09-20T10:00:00Z',
  };

  function account(createRun: () => Response) {
    return stubAccountFetch({
      ...signedInRoutes(USER_A),
      'GET /api/me/base-cvs': ok({ items: [SAVED_CV] }),
      'GET /api/me/job-postings': ok({
        items: [{ ...POSTING, id: 'account-posting-1', expires_at: null }],
      }),
      'GET /api/me/tailoring-runs': ok({ items: [], next_cursor: null }),
      'GET /api/base-cvs': ok({ items: [] }),
      'POST /api/me/tailoring-runs': createRun,
    });
  }

  beforeEach(() => {
    signInAs(USER_A);
  });

  it('a 429 with Retry-After: 120 disables it, sends nothing on click, and releases at +120 s', async () => {
    const fetch = account(() => tooMany(120));
    renderWithRouter('/');
    await settle(10); // the account boot needs fake time to load
    expect(launchButton()).toBeEnabled();

    const clickedAt = Date.now();
    fireEvent.click(launchButton());
    await settle();

    expect(callsTo(fetch, 'POST', '/api/me/tailoring-runs')).toHaveLength(1);
    expect(launchButton()).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent(/\d{2}:\d{2}/);
    fireEvent.click(launchButton());
    await settle();
    expect(callsTo(fetch, 'POST', '/api/me/tailoring-runs')).toHaveLength(1);

    const focused = document.activeElement;
    await advanceToward(clickedAt + 120_000, -1);
    expect(launchButton()).toBeDisabled();
    await advanceToward(clickedAt + 120_000, 0);
    expect(launchButton()).toBeEnabled();
    expect(screen.getByText('You can try again now.').closest('[role="status"]')).not.toBeNull();
    expect(screen.queryAllByText(WAIT_NAMED)).toHaveLength(0);
    expect(document.activeElement).toBe(focused);

    await advance(5000);
    expect(callsTo(fetch, 'POST', '/api/me/tailoring-runs')).toHaveLength(1);
  });

  it('a 503 holds nothing', async () => {
    const fetch = account(() =>
      jsonResponse(503, { error: { code: 'rate_limit_unavailable', message: 'Unavailable.' } }),
    );
    renderWithRouter('/');
    await settle(10); // the account boot needs fake time to load

    fireEvent.click(launchButton());
    await settle();

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(launchButton()).toBeEnabled();
    fireEvent.click(launchButton());
    await settle();
    expect(callsTo(fetch, 'POST', '/api/me/tailoring-runs')).toHaveLength(2);
  });
});

// --- AC-10: the provider-busy cooldown on a failed run ------------------------------------------------

const RUN_ID = 'run-page-fixture';

function stubFailedRun(overrides: Parameters<typeof makeRun>[0]) {
  return stubWorkspaceFetch({
    runDetail: {
      [RUN_ID]: () =>
        Promise.resolve(
          jsonResponse(
            200,
            makeRun({
              id: RUN_ID,
              status: 'failed',
              failure_reason: 'llm_rate_limited',
              retryable: true,
              ...overrides,
            }),
          ),
        ),
    },
  });
}

function tryAgain(): HTMLElement {
  return screen.getByRole('button', { name: /try again/i });
}

describe('AC-10 — Try again on a provider-busy run (RunPage)', () => {
  it('retry_not_before 45 s ahead disables Try again with the wait named, and enables it at the deadline', async () => {
    const fetchMock = stubFailedRun({
      completed_at: isoWhole(NOW - 15 * SECOND),
      retry_not_before: isoWhole(NOW + 45 * SECOND),
    });
    renderWithRouter(`/runs/${RUN_ID}/cv`);
    await settle();

    expect(tryAgain()).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent(/You can try again in 45 seconds/);
    fireEvent.click(tryAgain());
    await settle();
    expect(countCallsTo(fetchMock, '/api/tailoring-runs', 'POST')).toBe(0);

    const focused = document.activeElement;
    await advance(44_999);
    expect(tryAgain()).toBeDisabled();
    await advance(1);
    expect(tryAgain()).toBeEnabled();
    expect(screen.getByText('You can try again now.').closest('[role="status"]')).not.toBeNull();
    expect(screen.queryAllByText(WAIT_NAMED)).toHaveLength(0);
    expect(document.activeElement).toBe(focused);

    await advance(5000); // deadline + 5 s: no retry unless the user clicks (AC-22)
    expect(countCallsTo(fetchMock, '/api/tailoring-runs', 'POST')).toBe(0);
  });

  it('retry_not_before: null is unaffected: enabled at once (the control for the holds above)', async () => {
    stubFailedRun({ failure_reason: 'llm_rate_limited', retry_not_before: null });
    renderWithRouter(`/runs/${RUN_ID}/cv`);
    await settle();

    expect(tryAgain()).toBeEnabled();
  });

  it('another reason (llm_timed_out) is enabled at once', async () => {
    stubFailedRun({ failure_reason: 'llm_timed_out', retry_not_before: null });
    renderWithRouter(`/runs/${RUN_ID}/cv`);
    await settle();

    expect(tryAgain()).toBeEnabled();
  });

  it('F-18: a browser 10 minutes BEHIND waits at most the 60 s granted, not 11 minutes', async () => {
    const completed = NOW + 600 * SECOND; // the server's clock reads 10 minutes ahead of ours
    stubFailedRun({
      completed_at: isoWhole(completed),
      retry_not_before: isoWhole(completed + 60 * SECOND),
    });
    renderWithRouter(`/runs/${RUN_ID}/cv`);
    await settle();
    expect(tryAgain()).toBeDisabled();

    await advance(59_999);
    expect(tryAgain()).toBeDisabled();
    await advance(1);
    expect(tryAgain()).toBeEnabled();
  });

  it('F-18: a browser 10 minutes AHEAD waits 0', async () => {
    const completed = NOW - 600 * SECOND; // our clock reads 10 minutes ahead of the server's
    stubFailedRun({
      completed_at: isoWhole(completed),
      retry_not_before: isoWhole(completed + 60 * SECOND),
    });
    renderWithRouter(`/runs/${RUN_ID}/cv`);
    await settle();

    expect(tryAgain()).toBeEnabled();
  });
});

describe('AC-10 — Tailor again in the guest workspace after a provider-busy run', () => {
  beforeEach(() => {
    __resetForTests();
    authStore.signOut('expired');
  });

  function workspaceWithLatest(retryNotBefore: string | null) {
    const summary = makeRunSummary({
      id: RUN_ID,
      status: 'failed',
      failure_reason: 'llm_rate_limited',
      retryable: true,
      completed_at: isoWhole(NOW - 15 * SECOND),
      retry_not_before: retryNotBefore,
    });
    return stubWorkspaceFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [BASE_CV] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [POSTING] })),
      runsList: () => Promise.resolve(jsonResponse(200, { items: [summary] })),
      runDetail: {
        [RUN_ID]: () => Promise.resolve(jsonResponse(200, { ...summary, tailored_cv: null })),
      },
    });
  }

  it('is disabled with the wait named until retry_not_before, then enabled', async () => {
    const fetchMock = workspaceWithLatest(isoWhole(NOW + 45 * SECOND));
    renderWithRouter('/');
    await settle();

    expect(launchButton()).toBeDisabled();
    expect(launchButton()).toHaveAccessibleDescription(/You can try again in 45 seconds/);
    fireEvent.click(launchButton());
    await settle();
    expect(countCallsTo(fetchMock, '/api/tailoring-runs', 'POST')).toBe(0);

    await advance(45_000);
    expect(launchButton()).toBeEnabled();
    await advance(5000);
    expect(countCallsTo(fetchMock, '/api/tailoring-runs', 'POST')).toBe(0);
  });

  it('retry_not_before: null leaves it enabled (control)', async () => {
    workspaceWithLatest(null);
    renderWithRouter('/');
    await settle();

    expect(launchButton()).toBeEnabled();
  });
});
