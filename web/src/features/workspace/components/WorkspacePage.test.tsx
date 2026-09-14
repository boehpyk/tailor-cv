import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { renderWithRouter } from '@/test/render';
import {
  countCallsTo,
  jsonResponse,
  makeExtractedCv,
  makePostingSummary,
  makeRun,
  makeRunSummary,
  stubWorkspaceFetch,
} from '@/test/fixtures';

/**
 * F6 RED — the workspace's tab default/URL behaviour (AC-23), the launch → navigate flow and the
 * no-duplicate-`POST`-on-reattach guarantee, and the latest-run card's three states (AC-25).
 *
 * Written against the **spec**, mounted through `renderWithRouter` (F5b) so a launch's navigation
 * is a real router transition, not a prop the test would have to fake. `WorkspacePage.tsx`'s F5
 * skeleton hands every child inert values (`useWorkspaceTab(false)` always reporting `'base-cv'`,
 * `NOTHING_YET` progress, `run={{ kind: 'none' }}`, a `TailorLaunch` wired to `onLaunch: () =>
 * undefined`) and mounts the two real input panels with no query of its own — so every assertion
 * below fails for a real reason: the tab never actually moves, the button never actually launches
 * anything, and the latest-run card never actually says what the run is doing. None of these are
 * `ImportError`s — `WorkspacePage` renders today, just not correctly yet.
 */

const BASE_CV = makeExtractedCv();
const JOB_POSTING = makePostingSummary();

function stubReadyWorkspace(overrides: Parameters<typeof stubWorkspaceFetch>[0] = {}) {
  return stubWorkspaceFetch({
    baseCvs: () => Promise.resolve(jsonResponse(200, { items: [BASE_CV] })),
    jobPostings: () => Promise.resolve(jsonResponse(200, { items: [JOB_POSTING] })),
    runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
    ...overrides,
  });
}

describe('WorkspacePage — tab selection (AC-23)', () => {
  it('defaults to the Base CV tab when the session has no base CV', async () => {
    stubWorkspaceFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [] })),
      runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });

    renderWithRouter('/');

    expect(await screen.findByRole('tab', { name: 'Base CV' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
  });

  it('defaults to the Job posting tab when the session already has a base CV', async () => {
    stubWorkspaceFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [BASE_CV] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [] })),
      runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });

    renderWithRouter('/');

    expect(await screen.findByRole('tab', { name: 'Job posting' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
  });

  it('?tab=job-posting selects the job-posting tab even with no base CV', async () => {
    stubWorkspaceFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [] })),
      runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });

    renderWithRouter('/?tab=job-posting');

    expect(await screen.findByRole('tab', { name: 'Job posting' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
  });

  it('a half-typed posting survives switching to the Base CV tab and back', async () => {
    const user = userEvent.setup();
    // Both lists empty: an empty posting list is what makes `JobPostingPanel` show the input form
    // (a card would be rendered for an existing posting) — the very thing being typed into.
    stubWorkspaceFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [] })),
      runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });

    renderWithRouter('/?tab=job-posting');

    const textarea = await screen.findByLabelText('Job posting text');
    await user.type(textarea, 'Senior Widget Engineer, remote, apply within');

    await user.click(screen.getByRole('tab', { name: 'Base CV' }));
    // Genuinely switched, not merely clicked: the Job posting panel is now the *hidden* one — a
    // vacuity trap otherwise, since both panels stay mounted the whole time (AC-23) and a test that
    // only re-reads the textarea's value afterwards would pass even if the click did nothing at all,
    // because nothing would ever have unmounted it either way.
    expect(screen.getByRole('tabpanel', { name: 'Base CV' })).not.toHaveAttribute('hidden');
    expect(screen.getByRole('tabpanel', { name: 'Job posting', hidden: true })).toHaveAttribute(
      'hidden',
    );

    await user.click(screen.getByRole('tab', { name: 'Job posting' }));

    expect(screen.getByRole('tabpanel', { name: 'Job posting' })).not.toHaveAttribute('hidden');
    expect(screen.getByLabelText('Job posting text')).toHaveValue(
      'Senior Widget Engineer, remote, apply within',
    );
  });
});

describe('WorkspacePage — launch and reattachment (AC-25)', () => {
  it('clicking the launch control creates a run and navigates to /runs/{id}/cv', async () => {
    const user = userEvent.setup();
    const newRunId = 'run-just-created';
    const fetchMock = stubReadyWorkspace({
      createRun: () =>
        Promise.resolve(jsonResponse(202, makeRun({ id: newRunId, status: 'queued' }))),
      runDetail: {
        [newRunId]: () =>
          Promise.resolve(jsonResponse(200, makeRun({ id: newRunId, status: 'queued' }))),
      },
    });

    const { router } = renderWithRouter('/');

    const launchButton = await screen.findByRole('button', { name: /tailor/i });
    await user.click(launchButton);

    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/runs/${newRunId}/cv`);
    });
    expect(countCallsTo(fetchMock, '/api/tailoring-runs', 'POST')).toBe(1);
  });

  it('loading /runs/{id}/cv directly (a reattach/refresh) issues no POST', async () => {
    const runId = 'run-being-reattached-to';
    const fetchMock = stubWorkspaceFetch({
      runDetail: {
        [runId]: () => Promise.resolve(jsonResponse(200, makeRun({ id: runId, status: 'queued' }))),
      },
    });

    renderWithRouter(`/runs/${runId}/cv`);

    await screen.findByText('Waiting for a worker…');
    expect(countCallsTo(fetchMock, '/api/tailoring-runs', 'POST')).toBe(0);
  });
});

describe('WorkspacePage — the latest-run card (AC-25)', () => {
  it('an active latest run disables the launch and shows "In progress"', async () => {
    stubReadyWorkspace({
      runsList: () =>
        Promise.resolve(
          jsonResponse(200, { items: [makeRunSummary({ id: 'run-active', status: 'running' })] }),
        ),
    });

    renderWithRouter('/');

    expect(await screen.findByText(/in progress/i)).toBeInTheDocument();
    const launchButton = screen.getByRole('button', { name: /tailor/i });
    expect(launchButton).toBeDisabled();
  });

  it('a succeeded latest run offers "Open your tailored documents" and enables "Tailor again"', async () => {
    stubReadyWorkspace({
      runsList: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeRunSummary({
                id: 'run-succeeded',
                status: 'succeeded',
                tailored_cv_character_count: 10,
                cover_letter_character_count: 10,
              }),
            ],
          }),
        ),
    });

    renderWithRouter('/');

    expect(await screen.findByText(/open your tailored documents/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /tailor again/i })).toBeEnabled();
  });

  it("a failed latest run shows 1.3's failure copy", async () => {
    stubReadyWorkspace({
      runsList: () =>
        Promise.resolve(
          jsonResponse(200, {
            items: [
              makeRunSummary({
                id: 'run-failed',
                status: 'failed',
                failure_reason: 'llm_refused',
                retryable: false,
              }),
            ],
          }),
        ),
    });

    renderWithRouter('/');

    // failureCopy.ts's headline for `llm_refused` — byte-identical to 1.3's copy.
    expect(
      await screen.findByText('The model declined to rewrite this content.'),
    ).toBeInTheDocument();
  });
});
