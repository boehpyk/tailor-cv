import { QueryClient } from '@tanstack/react-query';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import {
  USER_A,
  USER_B,
  bearerFor,
  historyPage,
  makeAccountRun,
  makeHistoryEntry,
  signInAs,
  stubAccountFetch,
  tokenFor,
} from '@/test/accountFetch';
import { jsonResponse } from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

import type { RecordedCall } from '@/test/accountFetch';

/**
 * T30 RED — AC-43: account server state is keyed by user and cleared with the account. Sign in as
 * A, open a history run and the history list, log out through the header, sign in as B **in the
 * same tab**, and open the same URLs: A's documents and A's history are never rendered again — not
 * even for one frame, which a `MutationObserver` watching every DOM change after B signs in would
 * see. The client runs with `gcTime: Infinity` (2.1's trap: `gcTime: 0` would collect an unobserved
 * entry by itself and make the test pass for the collector's reason, not logout's).
 */

const A_DOCUMENT = 'A-ONLY tailored cv text';
const A_POSTING_TITLE = 'A-ONLY posting title';

function byBearer(call: RecordedCall, forA: () => Response, forB: () => Response): Response {
  return call.authorization === bearerFor(USER_A) ? forA() : forB();
}

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('Account cache across users in one tab (AC-43)', () => {
  it("never renders A's documents or history once B has signed in", async () => {
    const fetch = stubAccountFetch({
      'GET /api/auth/me': (call) =>
        byBearer(
          call,
          () => jsonResponse(200, USER_A),
          () => jsonResponse(200, USER_B),
        ),
      'GET /api/tailoring-runs': () => jsonResponse(200, { items: [] }),
      'POST /api/auth/logout': () => new Response(null, { status: 204 }),
      'GET /api/me/tailoring-runs/:id': (call) =>
        byBearer(
          call,
          () =>
            jsonResponse(
              200,
              makeAccountRun({
                id: 'run-1',
                status: 'succeeded',
                tailored_cv: A_DOCUMENT,
                cover_letter: 'A letter',
                tailored_cv_character_count: A_DOCUMENT.length,
                cover_letter_character_count: 8,
              }),
            ),
          () =>
            jsonResponse(404, {
              error: { code: 'tailoring_run_not_found', message: 'not found' },
            }),
        ),
      'GET /api/me/tailoring-runs/:id/exports': () => jsonResponse(200, { items: [] }),
      'GET /api/me/tailoring-runs': (call) =>
        byBearer(
          call,
          () =>
            jsonResponse(
              200,
              historyPage([
                makeHistoryEntry({
                  id: 'run-1',
                  posting: {
                    id: 'p1',
                    source: 'pasted',
                    title: A_POSTING_TITLE,
                    source_url: null,
                    preview: 'x',
                  },
                }),
              ]),
            ),
          () => jsonResponse(200, historyPage([])),
        ),
    });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
    });
    signInAs(USER_A);

    const { router } = renderWithRouter('/history', { queryClient });
    expect(await screen.findByText(A_POSTING_TITLE)).toBeInTheDocument();
    await act(async () => {
      await router.navigate('/history/run-1/cv');
    });
    expect(await screen.findByText(A_DOCUMENT)).toBeInTheDocument();

    await userEvent.setup().click(screen.getByRole('button', { name: 'Log out' }));
    await waitFor(() => {
      expect(authStore.getSnapshot().status).toBe('anonymous');
    });
    expect(
      queryClient.getQueryCache().findAll({ queryKey: ['auth', 'account', USER_A.id] }),
    ).toHaveLength(0);

    let sawA = false;
    const observer = new MutationObserver(() => {
      const text = document.body.textContent;
      if (text.includes(A_DOCUMENT) || text.includes(A_POSTING_TITLE)) {
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
        await router.navigate('/history/run-1/cv');
      });
      await waitFor(() => {
        expect(
          fetch.calls.some(
            (call) =>
              call.path === '/api/me/tailoring-runs/run-1' &&
              call.authorization === bearerFor(USER_B),
          ),
        ).toBe(true);
      });
      await act(async () => {
        await router.navigate('/history');
      });
      await waitFor(() => {
        expect(
          fetch.calls.some(
            (call) =>
              call.path === '/api/me/tailoring-runs' && call.authorization === bearerFor(USER_B),
          ),
        ).toBe(true);
      });
      expect(await screen.findByText('Nothing tailored yet')).toBeInTheDocument();
    } finally {
      observer.disconnect();
    }

    expect(sawA).toBe(false);
    expect(screen.queryByText(A_DOCUMENT)).not.toBeInTheDocument();
    expect(screen.queryByText(A_POSTING_TITLE)).not.toBeInTheDocument();
  });
});
