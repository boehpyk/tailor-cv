import { act, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { renderWithRouter } from '@/test/render';
import { jsonResponse, makeRun, stubWorkspaceFetch } from '@/test/fixtures';

/**
 * Flush pending microtasks under fake timers without waiting on a real clock — same helper as
 * `TailorPanel.test.tsx`'s (T44's own test for the transient-network-failure state).
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

/**
 * F6 RED — the run page's four states plus the three error kinds (feature-spec AC-26;
 * technical-plan "Loading / error / empty / success" → "Run page").
 *
 * Written against the **spec**, not `RunPage.tsx`'s F5 skeleton, which renders one empty
 * `<div role="status" />` and reads no params and no query — so every assertion below fails on a
 * real "text not found" (the copy simply is not there), never an `ImportError`.
 *
 * Mounted through `renderWithRouter` (F5b) at `/runs/{id}/cv`, the real route table, because
 * `RunPage` reads `:runId`/`:document` from the URL and AC-25's "no second `POST` on reattach"
 * companion assertion (`countCallsTo(..., 'POST')` staying 0) only means anything against the real
 * router, not a bare `<RunPage />`.
 */

const RUN_ID = 'run-page-fixture';

function renderRunPage(document: 'cv' | 'cover_letter' = 'cv') {
  return renderWithRouter(`/runs/${RUN_ID}/${document}`);
}

describe('RunPage', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows the loading copy while the run has not answered yet', () => {
    stubWorkspaceFetch({ runDetail: { [RUN_ID]: () => new Promise<Response>(() => undefined) } });

    renderRunPage();

    // Byte-identical to 1.3's `TailorPanel` loading line.
    expect(screen.getByRole('status')).toHaveTextContent('Loading your tailoring run…');
  });

  it('a queued run shows the stepper working, with 1.3\'s "Waiting for a worker…" and no retry control', async () => {
    stubWorkspaceFetch({
      runDetail: {
        [RUN_ID]: () =>
          Promise.resolve(jsonResponse(200, makeRun({ id: RUN_ID, status: 'queued' }))),
      },
    });

    renderRunPage();

    expect(await screen.findByText('Waiting for a worker…')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument();
  });

  it('a running run shows 1.3\'s "Tailoring with Gemini…" and no retry control', async () => {
    stubWorkspaceFetch({
      runDetail: {
        [RUN_ID]: () =>
          Promise.resolve(jsonResponse(200, makeRun({ id: RUN_ID, status: 'running' }))),
      },
    });

    renderRunPage();

    expect(await screen.findByText(/Tailoring with Gemini/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument();
  });

  it("a failed, retryable run shows 1.3's failure notice with Try again", async () => {
    stubWorkspaceFetch({
      runDetail: {
        [RUN_ID]: () =>
          Promise.resolve(
            jsonResponse(
              200,
              makeRun({
                id: RUN_ID,
                status: 'failed',
                failure_reason: 'llm_timed_out',
                retryable: true,
              }),
            ),
          ),
      },
    });

    renderRunPage();

    expect(await screen.findByText('That took too long.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });

  it("a failed, non-retryable run shows 1.3's failure notice without Try again", async () => {
    stubWorkspaceFetch({
      runDetail: {
        [RUN_ID]: () =>
          Promise.resolve(
            jsonResponse(
              200,
              makeRun({
                id: RUN_ID,
                status: 'failed',
                failure_reason: 'llm_refused',
                retryable: false,
              }),
            ),
          ),
      },
    });

    renderRunPage();

    expect(
      await screen.findByText('The model declined to rewrite this content.'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument();
  });

  it('a succeeded run renders the tailored documents (TailoredDocumentsPreview, the F7 placeholder)', async () => {
    stubWorkspaceFetch({
      runDetail: {
        [RUN_ID]: () =>
          Promise.resolve(
            jsonResponse(
              200,
              makeRun({
                id: RUN_ID,
                status: 'succeeded',
                tailored_cv: 'my tailored cv text',
                cover_letter: 'my cover letter text',
                tailored_cv_character_count: 20,
                cover_letter_character_count: 21,
              }),
            ),
          ),
      },
    });

    renderRunPage();

    expect(await screen.findByText('my tailored cv text')).toBeInTheDocument();
    expect(screen.getByText('my cover letter text')).toBeInTheDocument();
  });

  it('404 shows "We couldn\'t find that run." and a link home', async () => {
    stubWorkspaceFetch({
      runDetail: {
        [RUN_ID]: () =>
          Promise.resolve(
            jsonResponse(404, {
              error: { code: 'tailoring_run_not_found', message: "We couldn't find that run." },
            }),
          ),
      },
    });

    renderRunPage();

    expect(await screen.findByText("We couldn't find that run.")).toBeInTheDocument();
    const homeLink = screen.getByRole('link', { name: /home|workspace/i });
    expect(homeLink).toHaveAttribute('href', '/');
  });

  it('401 shows the session-expired copy and a link home', async () => {
    stubWorkspaceFetch({
      runDetail: {
        [RUN_ID]: () =>
          Promise.resolve(
            jsonResponse(401, {
              error: { code: 'guest_session_expired', message: 'Your session has expired.' },
            }),
          ),
      },
    });

    renderRunPage();

    // apiErrorCopy.ts's `runReadErrorCopy` — the same sentence used elsewhere for this code.
    expect(
      await screen.findByText('Your session has expired. Upload your CV again.'),
    ).toBeInTheDocument();
    const homeLink = screen.getByRole('link', { name: /home|workspace/i });
    expect(homeLink).toHaveAttribute('href', '/');
  });

  it("a network failure shows 1.3's unreadable copy with Check again", async () => {
    // `useTailoringRun`'s own `retry` (its docstring: it overrides the test client's `retry:
    // false`) retries a non-4xx failure up to `MAX_TRANSIENT_RETRIES` (3) with TanStack's default
    // backoff (1000 * 2 ** failureCount): 1s, 2s, 4s — about 7s before the query settles into its
    // error state. `findByText`'s default real-timer wait gives up at 1s, long before that, so this
    // drives a faked clock forward by hand instead, the same way `TailorPanel.test.tsx`'s
    // transient-network-retry-exhaustion test (T44 gap (b)) does. Unlike that test, the very first
    // fetch here is the one that rejects — there is no prior successful poll to wait
    // `POLL_INTERVAL_MS` for — so only the three backoffs need advancing.
    vi.useFakeTimers();
    stubWorkspaceFetch({
      runDetail: {
        [RUN_ID]: () => Promise.reject(new TypeError('Failed to fetch')),
      },
    });

    renderRunPage();
    await flushMicrotasks();

    await advance(1000); // retry #1 backoff (failureCount 0 -> 2**0 * 1000ms)
    await advance(2000); // retry #2 backoff (failureCount 1 -> 2**1 * 1000ms)
    await advance(4000); // retry #3 backoff (failureCount 2 -> 2**2 * 1000ms) - retries exhausted
    await advance(1000); // drain the settled error state into the DOM

    expect(
      screen.getByText('We lost contact with your tailoring run. It may still be working.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /check again/i })).toBeInTheDocument();
  });

  it('the working copy and every failure copy are all different strings', async () => {
    // One literal sentence per state (each already asserted individually above); the point of this
    // test is that `RunPage` actually renders all five as distinct text, not that they are
    // different strings in the abstract — five copies of one generic "Something went wrong" would
    // fail every case above's `getByText` search on the exact sentence, but would still pass a
    // weaker check that merely counted rendered nodes.
    const cases: ReadonlyArray<{ stub: () => Promise<Response>; expectedText: string }> = [
      {
        stub: () => Promise.resolve(jsonResponse(200, makeRun({ id: RUN_ID, status: 'running' }))),
        expectedText: 'Tailoring with Gemini…',
      },
      {
        stub: () =>
          Promise.resolve(
            jsonResponse(
              200,
              makeRun({
                id: RUN_ID,
                status: 'failed',
                failure_reason: 'llm_refused',
                retryable: false,
              }),
            ),
          ),
        expectedText: 'The model declined to rewrite this content.',
      },
      {
        stub: () =>
          Promise.resolve(
            jsonResponse(404, {
              error: { code: 'tailoring_run_not_found', message: "We couldn't find that run." },
            }),
          ),
        expectedText: "We couldn't find that run.",
      },
      {
        stub: () =>
          Promise.resolve(
            jsonResponse(401, { error: { code: 'guest_session_expired', message: 'expired' } }),
          ),
        expectedText: 'Your session has expired. Upload your CV again.',
      },
      {
        stub: () => Promise.reject(new TypeError('Failed to fetch')),
        expectedText: 'We lost contact with your tailoring run. It may still be working.',
      },
    ];

    const networkFailureText = 'We lost contact with your tailoring run. It may still be working.';

    for (const { stub, expectedText } of cases) {
      stubWorkspaceFetch({ runDetail: { [RUN_ID]: stub } });

      if (expectedText === networkFailureText) {
        // This one case can't use `findByText` with real timers: `useTailoringRun`'s retry takes
        // ~7s of backoff (1s/2s/4s) to settle a non-4xx failure, well past `findByText`'s default
        // 1s wait. Same fake-timer drive as the dedicated network-failure test above and
        // `TailorPanel.test.tsx`'s T44 gap (b) test.
        vi.useFakeTimers();
        const { unmount } = renderRunPage();
        await flushMicrotasks();
        await advance(1000); // retry #1 backoff
        await advance(2000); // retry #2 backoff
        await advance(4000); // retry #3 backoff - retries exhausted
        await advance(1000); // drain the settled error state into the DOM
        expect(screen.getByText(expectedText, { exact: false })).toBeInTheDocument();
        unmount();
        vi.useRealTimers();
      } else {
        const { unmount, findByText } = renderRunPage();
        expect(await findByText(expectedText, { exact: false })).toBeInTheDocument();
        unmount();
      }
    }

    const distinct = new Set(cases.map((c) => c.expectedText));
    expect(distinct.size).toBe(cases.length);
  });
});
