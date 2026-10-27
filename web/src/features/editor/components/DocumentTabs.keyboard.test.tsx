import { QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RouterProvider, createMemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { WorkspaceScopeProvider } from '@/features/scope/WorkspaceScope';
import { GUEST_SCOPE, scopeMap, type WorkspaceScope } from '@/features/scope/scopeMap';
import { tailoringRunQueryKey } from '@/features/tailoring/hooks/useTailoringRun';
import { USER_A, makeAccountRun, signInAs, stubAccountFetch } from '@/test/accountFetch';
import { createAppQueryClient } from '@/queryClient';

import { DocumentWorkspace } from './DocumentWorkspace';

/**
 * T21 RED (web-design-refresh AC-19): the document tablist is keyboard-operable like `InputTabs`.
 * With focus on the selected tab, ArrowRight/ArrowLeft (wrapping), Home and End select the
 * corresponding tab: the URL changes to that document and focus lands on the newly selected tab.
 * A guest run stays under `/runs/:id/…`, an account run under `/history/:id/…`; and a tab switch
 * is not "leaving", so the unsaved-text prompt (`window.confirm`) never fires.
 *
 * Written from the spec, not from `DocumentTabs.tsx` (a `<Link role="tab">` with roving tabIndex and
 * no key handler). The no-prompt test also goes red today, on the missing navigation; once GREEN it
 * guards the leave-guard (`useBlocker` in `DocumentWorkspace`) against treating a key switch as leaving.
 */

const RUN_ID = 'r1';
type Doc = 'cv' | 'cover_letter';

const SCOPES: readonly { name: string; scope: WorkspaceScope; prefix: string }[] = [
  { name: 'guest run', scope: GUEST_SCOPE, prefix: '/runs' },
  { name: 'history run', scope: { kind: 'account', userId: USER_A.id }, prefix: '/history' },
];

function setup(scope: WorkspaceScope, prefix: string, initial: Doc) {
  const run = makeAccountRun({
    id: RUN_ID,
    status: 'succeeded',
    tailored_cv: 'CV seed text',
    cover_letter: 'Letter seed text',
    tailored_cv_character_count: 12,
    cover_letter_character_count: 16,
  });
  const queryClient = createAppQueryClient();
  queryClient.setQueryData(tailoringRunQueryKey(RUN_ID, scopeMap(scope)), run);
  // Every request is unrouted, so it rejects: an autosave of an edit fails and the text stays unsaved.
  stubAccountFetch({});
  const router = createMemoryRouter(
    [
      { path: '/', element: <p>the workspace</p> },
      {
        path: `${prefix}/:runId/:document`,
        element: (
          <WorkspaceScopeProvider scope={scope}>
            <DocumentWorkspace run={run} />
          </WorkspaceScopeProvider>
        ),
      },
    ],
    { initialEntries: [`${prefix}/${RUN_ID}/${initial}`] },
  );
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

function tabFor(kind: Doc): HTMLElement {
  return screen.getByRole('tab', { name: kind === 'cv' ? /^cv/i : /cover letter/i });
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  __resetForTests();
});

const KEYS: readonly { key: string; from: Doc; to: Doc }[] = [
  { key: '{ArrowRight}', from: 'cv', to: 'cover_letter' },
  { key: '{ArrowRight}', from: 'cover_letter', to: 'cv' },
  { key: '{ArrowLeft}', from: 'cover_letter', to: 'cv' },
  { key: '{ArrowLeft}', from: 'cv', to: 'cover_letter' },
  { key: '{End}', from: 'cv', to: 'cover_letter' },
  { key: '{Home}', from: 'cover_letter', to: 'cv' },
];

describe.each(SCOPES)('Document tabs keyboard on a $name', ({ scope, prefix }) => {
  it.each(KEYS)('$key from $from selects $to and focuses it', async ({ key, from, to }) => {
    const router = setup(scope, prefix, from);
    const user = userEvent.setup();
    act(() => {
      tabFor(from).focus();
    });
    expect(tabFor(from)).toHaveFocus();

    await user.keyboard(key);

    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`${prefix}/${RUN_ID}/${to}`);
    });
    await waitFor(() => {
      expect(tabFor(to)).toHaveFocus();
    });
    expect(tabFor(to)).toHaveAttribute('aria-selected', 'true');
  });

  it('Home on the first tab stays put', async () => {
    const router = setup(scope, prefix, 'cv');
    const user = userEvent.setup();
    act(() => {
      tabFor('cv').focus();
    });

    await user.keyboard('{Home}');

    expect(router.state.location.pathname).toBe(`${prefix}/${RUN_ID}/cv`);
    expect(tabFor('cv')).toHaveFocus();
  });

  it('switching documents by keyboard with unsaved text never asks to leave', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const router = setup(scope, prefix, 'cv');
    const user = userEvent.setup();
    const cvPane = document.querySelector<HTMLElement>('[data-document="cv"] .ProseMirror');
    if (cvPane === null) {
      throw new Error('no CV editor rendered');
    }
    await user.click(cvPane);
    await user.type(cvPane, ' edited');
    act(() => {
      tabFor('cv').focus();
    });

    await user.keyboard('{ArrowRight}');

    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`${prefix}/${RUN_ID}/cover_letter`);
    });
    expect(confirmSpy).not.toHaveBeenCalled();
  });
});
