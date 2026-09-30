import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { WorkspaceScopeProvider } from '@/features/scope/WorkspaceScope';
import { scopeMap } from '@/features/scope/scopeMap';
import { tailoringRunQueryKey } from '@/features/tailoring/hooks/useTailoringRun';
import {
  USER_A,
  makeAccountRun,
  ok,
  signInAs,
  signedInRoutes,
  stubAccountFetch,
} from '@/test/accountFetch';
import { renderWithRouter } from '@/test/render';

import { DocumentWorkspace, UNSAVED_LEAVE_PROMPT } from './DocumentWorkspace';

/**
 * T35 (test-after) — the bug T32 fixed, pinned: `DocumentTabs` and the editor's leave-guard used to
 * hard-code `/runs/…`, so on a history run (`/history/:id`) the **Cover letter** tab jumped to the
 * guest route, and switching documents with unsaved text counted as leaving.
 *
 * **Observed red, 2026-09-30, and restored byte-exact** (`git diff --stat -- src` empty; green
 * after): with `to={runLink(map, runId, kind)}` in `DocumentTabs.tsx` put back to
 * `` to={`/runs/${runId}/${kind}`} ``, the first test failed on the tab's `href` (`toHaveAttribute`:
 * expected `/history/run-1/cover_letter`, received `/runs/run-1/cover_letter`); with the leave-guard's
 * two `runLink(map, runId, …)` comparisons in `DocumentWorkspace.tsx` put back to `/runs/…`, the
 * second failed at `expect(confirmSpy).not.toHaveBeenCalled()` — "called 1 times": a tab switch on a
 * history run was treated as leaving.
 */

const RUN_ID = 'run-1';

function succeededRun() {
  return makeAccountRun({
    id: RUN_ID,
    status: 'succeeded',
    tailored_cv: 'CV seed text',
    cover_letter: 'Letter seed text',
    tailored_cv_character_count: 12,
    cover_letter_character_count: 16,
  });
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  __resetForTests();
});

describe('Document tabs on a history run follow the account scope', () => {
  it('the Cover letter tab links to /history/{id}/cover_letter, never to /runs/…', async () => {
    stubAccountFetch({
      ...signedInRoutes(USER_A),
      'GET /api/me/tailoring-runs/:id': ok(succeededRun()),
      'GET /api/me/tailoring-runs/:id/exports': ok({ items: [] }),
    });
    const user = userEvent.setup();

    const { router, container } = renderWithRouter(`/history/${RUN_ID}/cv`);
    const coverLetter = await screen.findByRole('tab', { name: /cover letter/i });

    expect(coverLetter).toHaveAttribute('href', `/history/${RUN_ID}/cover_letter`);
    expect(container.querySelectorAll('a[href^="/runs/"]')).toHaveLength(0);
    await user.click(coverLetter);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/history/${RUN_ID}/cover_letter`);
    });
  });

  it('switching documents with unsaved text is not leaving; going home still asks', async () => {
    const account = scopeMap({ kind: 'account', userId: USER_A.id });
    const run = succeededRun();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
    });
    queryClient.setQueryData(tailoringRunQueryKey(RUN_ID, account), run);
    stubAccountFetch({
      'PUT /api/me/tailoring-runs/:id/documents/:kind': () =>
        Promise.reject(new TypeError('Failed to fetch')),
    });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const router = createMemoryRouter(
      [
        { path: '/', element: <p>the workspace</p> },
        {
          path: '/history/:runId/:document',
          element: (
            <WorkspaceScopeProvider scope={{ kind: 'account', userId: USER_A.id }}>
              <DocumentWorkspace run={run} />
            </WorkspaceScopeProvider>
          ),
        },
      ],
      { initialEntries: [`/history/${RUN_ID}/cv`] },
    );
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    const cvPane = document.querySelector<HTMLElement>('[data-document="cv"] .ProseMirror');
    if (cvPane === null) {
      throw new Error('no CV editor rendered');
    }
    await user.click(cvPane);
    await user.type(cvPane, ' edited');

    await act(async () => {
      await router.navigate(`/history/${RUN_ID}/cover_letter`);
    });
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(router.state.location.pathname).toBe(`/history/${RUN_ID}/cover_letter`);

    await act(async () => {
      await router.navigate('/');
    });
    expect(confirmSpy).toHaveBeenCalledWith(UNSAVED_LEAVE_PROMPT);
  });
});
