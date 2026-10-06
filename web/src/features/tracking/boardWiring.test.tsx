import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import {
  USER_A,
  USER_B,
  bearerFor,
  callsTo,
  historyPage,
  makeAccountRun,
  makeHistoryEntry,
  ok,
  signInAs,
  stubAccountFetch,
  tokenFor,
} from '@/test/accountFetch';
import { makeRun } from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

import { boardKey } from './hooks/trackingKeys';
import {
  APPLICATIONS_PATH,
  BOARD_PATH,
  boardServer,
  makeCard,
  newClient,
  otherCard,
} from './test/support';

/**
 * T27 RED — the wiring: the `/board` route and its guard, the header's **Board** link, the control
 * in a history row and on an account run page (AC-38's placement, in both directions), the history
 * delete invalidating the board, and AC-39's two-users-in-one-tab proof.
 *
 * Mounted through the real route table, so every test here is red until T28 wires the route, the
 * header link and the control — which is the intended red. Where a test is about something *absent*
 * (a guest's header, a queued row), it is paired with a positive in the same test, because an
 * unwired app satisfies every absence.
 */

const HISTORY = '/api/me/tailoring-runs';

function entry(id: string, title: string, overrides: Parameters<typeof makeHistoryEntry>[0] = {}) {
  return makeHistoryEntry({
    id,
    posting: { id: `p-${id}`, source: 'pasted', title, source_url: null, preview: 'x' },
    ...overrides,
  });
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

// --- the route ------------------------------------------------------------------------------------

describe('/board (AC-32)', () => {
  it('signed in, it mounts the board in account scope: the board is read with the bearer and shown', async () => {
    const server = boardServer({ 'user-a': [makeCard({ tailoring_run_id: 'run-5' })] });
    const fetch = stubAccountFetch(server.routes());

    renderWithRouter('/board', { queryClient: newClient() });

    expect(await screen.findByText('Platform role at Acme')).toBeInTheDocument();
    expect(callsTo(fetch, 'GET', BOARD_PATH)[0]?.authorization).toBe(bearerFor(USER_A));
    const hrefs = screen.getAllByRole('link').map((link) => link.getAttribute('href'));
    expect(hrefs).toContain('/history/run-5/cv');
  });

  it('a guest is sent to /login?next=/board and no board is read', async () => {
    __resetForTests();
    authStore.signOut('expired');
    const server = boardServer({});
    const fetch = stubAccountFetch({
      ...server.routes(),
      'GET /api/job-postings': ok({ items: [] }),
    });

    const { router } = renderWithRouter('/board');

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/login');
    });
    expect(new URLSearchParams(router.state.location.search).get('next')).toBe('/board');
    expect(callsTo(fetch, 'GET', BOARD_PATH)).toHaveLength(0);
  });
});

describe("The header's Board link (AC-32)", () => {
  it('links to /board when signed in', async () => {
    stubAccountFetch(boardServer({ 'user-a': [] }).routes());

    renderWithRouter('/history');

    const banner = await screen.findByRole('banner');
    const link = await within(banner).findByRole('link', { name: 'Board' });
    expect(link).toHaveAttribute('href', '/board');
  });

  it('has no Board link for a guest (green on arrival — paired with the test above)', async () => {
    __resetForTests();
    authStore.signOut('expired');
    stubAccountFetch({
      'GET /api/base-cvs': ok({ items: [] }),
      'GET /api/job-postings': ok({ items: [] }),
      'GET /api/tailoring-runs': ok({ items: [] }),
    });

    renderWithRouter('/');

    await screen.findByRole('tab', { name: 'Base CV' });
    expect(screen.queryByRole('link', { name: 'Board' })).not.toBeInTheDocument();
  });
});

// --- AC-38: where the control is -------------------------------------------------------------------

function rowOf(title: string): HTMLElement {
  const row = screen.getAllByRole('listitem').find((item) => item.textContent.includes(title));
  if (row === undefined) {
    throw new Error(`no history row contains ${JSON.stringify(title)}`);
  }
  return row;
}

describe('The history row (AC-38)', () => {
  function serveHistory() {
    const server = boardServer({
      'user-a': [makeCard({ tailoring_run_id: 'on-board', stage: 'interviewing' })],
    });
    const fetch = stubAccountFetch({
      ...server.routes(),
      [`GET ${HISTORY}`]: ok(
        historyPage([
          entry('on-board', 'Role on the board'),
          entry('off-board', 'Role off the board'),
          entry('still-queued', 'Role still queued', { status: 'queued', completed_at: null }),
          entry('did-fail', 'Role that failed', {
            status: 'failed',
            failure_reason: 'llm_unavailable',
            retryable: true,
          }),
        ]),
      ),
    });
    return { server, fetch };
  }

  it('is on succeeded rows only: a badge where the run is on the board, "Add to board" where it is not, nothing on queued or failed rows', async () => {
    serveHistory();

    renderWithRouter('/history');
    await screen.findByText('Role off the board');

    const onBoard = await within(rowOf('Role on the board')).findByRole('link', {
      name: 'On your board · Interviewing',
    });
    expect(onBoard).toHaveAttribute('href', '/board');
    expect(
      within(rowOf('Role on the board')).queryByRole('button', { name: 'Add to board' }),
    ).not.toBeInTheDocument();
    expect(
      within(rowOf('Role off the board')).getByRole('button', { name: 'Add to board' }),
    ).toBeEnabled();
    for (const title of ['Role still queued', 'Role that failed']) {
      expect(
        within(rowOf(title)).queryByRole('button', { name: 'Add to board' }),
      ).not.toBeInTheDocument();
      expect(
        within(rowOf(title)).queryByRole('link', { name: /on your board/i }),
      ).not.toBeInTheDocument();
    }
  });

  it('Add to board posts the row\'s run id, then the row shows "On your board · To apply"', async () => {
    const { fetch } = serveHistory();
    const user = userEvent.setup();
    renderWithRouter('/history');
    await screen.findByText('Role off the board');

    await user.click(
      within(rowOf('Role off the board')).getByRole('button', { name: 'Add to board' }),
    );

    expect(
      await within(rowOf('Role off the board')).findByRole('link', {
        name: 'On your board · To apply',
      }),
    ).toHaveAttribute('href', '/board');
    expect(callsTo(fetch, 'POST', APPLICATIONS_PATH)[0]?.body).toEqual({
      tailoring_run_id: 'off-board',
    });
  });

  it('tracking a run refreshes the history pages (AC-39)', async () => {
    const { fetch } = serveHistory();
    const user = userEvent.setup();
    renderWithRouter('/history');
    await screen.findByText('Role off the board');
    const before = callsTo(fetch, 'GET', HISTORY).length;

    await user.click(
      within(rowOf('Role off the board')).getByRole('button', { name: 'Add to board' }),
    );
    await within(rowOf('Role off the board')).findByRole('link', { name: /on your board/i });

    await waitFor(() => {
      expect(callsTo(fetch, 'GET', HISTORY).length).toBeGreaterThan(before);
    });
  });

  it('deleting a history entry invalidates the board (AC-39)', async () => {
    const server = boardServer({ 'user-a': [makeCard({ tailoring_run_id: 'off-board' })] });
    stubAccountFetch({
      ...server.routes(),
      [`GET ${HISTORY}`]: ok(historyPage([entry('off-board', 'Role off the board')])),
      'DELETE /api/me/tailoring-runs/:id': () => new Response(null, { status: 204 }),
    });
    const client = newClient();
    const user = userEvent.setup();
    const { router } = renderWithRouter('/board', { queryClient: client });
    await screen.findByText('Platform role at Acme');
    expect(client.getQueryState(boardKey('user-a'))?.isInvalidated).toBe(false);
    await act(async () => {
      await router.navigate('/history');
    });
    await screen.findByText('Role off the board');

    await user.click(within(rowOf('Role off the board')).getByRole('button', { name: 'Delete' }));
    const dialog = await screen.findByRole('alertdialog');
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }));

    await waitFor(() => {
      expect(client.getQueryState(boardKey('user-a'))?.isInvalidated).toBe(true);
    });
  });
});

describe('The run page (AC-38)', () => {
  function succeededAccountRun() {
    return makeAccountRun({
      id: 'run-1',
      status: 'succeeded',
      tailored_cv: 'my tailored cv text',
      cover_letter: 'my cover letter text',
      tailored_cv_character_count: 19,
      cover_letter_character_count: 20,
      completed_at: '2026-09-20T10:00:09Z',
    });
  }

  it('an account run that succeeded offers "Add to board"', async () => {
    const server = boardServer({ 'user-a': [] });
    stubAccountFetch({
      ...server.routes(),
      'GET /api/me/tailoring-runs/:id': ok(succeededAccountRun()),
      'GET /api/me/tailoring-runs/:id/exports': ok({ items: [] }),
    });

    renderWithRouter('/history/run-1/cv');

    expect(await screen.findByRole('button', { name: 'Add to board' })).toBeEnabled();
  });

  it('an account run already on the board shows the badge instead', async () => {
    const server = boardServer({
      'user-a': [makeCard({ tailoring_run_id: 'run-1', stage: 'offer' })],
    });
    stubAccountFetch({
      ...server.routes(),
      'GET /api/me/tailoring-runs/:id': ok(succeededAccountRun()),
      'GET /api/me/tailoring-runs/:id/exports': ok({ items: [] }),
    });

    renderWithRouter('/history/run-1/cv');

    expect(await screen.findByRole('link', { name: 'On your board · Offer' })).toHaveAttribute(
      'href',
      '/board',
    );
    expect(screen.queryByRole('button', { name: 'Add to board' })).not.toBeInTheDocument();
  });

  it('an account run that is still queued does not (the page itself rendered)', async () => {
    const server = boardServer({ 'user-a': [] });
    stubAccountFetch({
      ...server.routes(),
      'GET /api/me/tailoring-runs/:id': ok(makeAccountRun({ id: 'run-1', status: 'queued' })),
    });

    renderWithRouter('/history/run-1/cv');

    expect(await screen.findByText('Waiting for a worker…')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add to board' })).not.toBeInTheDocument();
  });

  it('a guest run page never does — even with an account signed in — and reads no board', async () => {
    const server = boardServer({ 'user-a': [] });
    const fetch = stubAccountFetch({
      ...server.routes(),
      'GET /api/tailoring-runs/:id': ok(
        makeRun({
          id: 'guest-run',
          status: 'succeeded',
          tailored_cv: 'my guest cv text',
          cover_letter: 'my guest letter',
          tailored_cv_character_count: 15,
          cover_letter_character_count: 15,
          completed_at: '2026-09-12T10:00:09Z',
        }),
      ),
      'GET /api/tailoring-runs/:id/exports': ok({ items: [] }),
    });

    renderWithRouter('/runs/guest-run/cv');

    expect(await screen.findByText('my guest cv text')).toBeInTheDocument();
    await screen.findByRole('button', { name: 'PDF' });
    expect(screen.queryByRole('button', { name: 'Add to board' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /on your board/i })).not.toBeInTheDocument();
    expect(callsTo(fetch, 'GET', BOARD_PATH)).toHaveLength(0);
  });
});

// --- AC-39: two users, one tab ----------------------------------------------------------------------

describe('Account cache across users in one tab (AC-39)', () => {
  it("never renders A's cards once B has signed in", async () => {
    const A_ONLY = 'A-ONLY application title';
    const server = boardServer({
      'user-a': [makeCard({ title: A_ONLY })],
      'user-b': [otherCard(7, { title: 'B-ONLY application title' })],
    });
    const fetch = stubAccountFetch({
      ...server.routes(),
      'POST /api/auth/logout': () => new Response(null, { status: 204 }),
    });
    const client = newClient();
    const user = userEvent.setup();

    const { router } = renderWithRouter('/board', { queryClient: client });
    expect(await screen.findByText(A_ONLY)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Log out' }));
    await waitFor(() => {
      expect(authStore.getSnapshot().status).toBe('anonymous');
    });
    expect(
      client.getQueryCache().findAll({ queryKey: ['auth', 'account', 'user-a'] }),
    ).toHaveLength(0);
    expect(client.getQueryData(boardKey('user-a'))).toBeUndefined();

    let sawA = false;
    const observer = new MutationObserver(() => {
      if (document.body.textContent.includes(A_ONLY)) {
        sawA = true;
      }
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    try {
      act(() => {
        authStore.setAuthenticated({
          access_token: tokenFor(USER_B),
          token_type: 'Bearer',
          expires_in: 900,
          user: USER_B,
        });
      });
      await act(async () => {
        await router.navigate('/board');
      });
      expect(await screen.findByText('B-ONLY application title')).toBeInTheDocument();
    } finally {
      observer.disconnect();
    }

    expect(sawA).toBe(false);
    expect(screen.queryByText(A_ONLY)).not.toBeInTheDocument();
    const boardReadsAfterA = callsTo(fetch, 'GET', BOARD_PATH).filter(
      (call) => call.authorization === bearerFor(USER_B),
    );
    expect(boardReadsAfterA.length).toBeGreaterThan(0);
  });
});
