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
  makeHistoryEntry,
  signInAs,
  stubAccountFetch,
  tokenFor,
} from '@/test/accountFetch';
import { jsonResponse, makePostingSummary } from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

import { KEEP_LABEL, KEEP_PENDING_LABEL, OFFER_REGION_LABEL } from './claimCopy';
import { CLAIM_PATH, claimResult, guestCv, guestRun, newClient } from './test/support';

import type { RecordedCall } from '@/test/accountFetch';

/**
 * T30 RED — the claim through the whole app, on `/` (AC-38's "on `/` the account workspace now
 * lists the CV and the latest run") and across two users in one tab (AC-42, `gcTime: Infinity`).
 *
 * Mounted through the real route table with every request recorded. The server's answers are
 * **stateful**: before the claim the account holds nothing and the browser holds guest work; after
 * it, the account holds the CV and a run and the guest lists are empty — which is what makes "the
 * workspace re-reads the right things" observable rather than assumed.
 */

const A_CV = 'A-ONLY-claimed.pdf';

function savedCv(filename: string) {
  return {
    id: 'saved-1',
    label: null,
    original_filename: filename,
    content_type: 'application/pdf',
    size_bytes: 2048,
    status: 'extracted',
    character_count: 512,
    failure_reason: null,
    failure_message: null,
    uploaded_at: '2026-09-20T10:00:00Z',
  };
}

function statefulServer(opts: { readonly hangClaim?: boolean } = {}) {
  const state: { claimed: boolean; release: (response: Response) => void } = {
    claimed: false,
    release: () => undefined,
  };
  const byUser = (call: RecordedCall): typeof USER_A =>
    call.authorization === bearerFor(USER_B) ? USER_B : USER_A;
  const fetch = stubAccountFetch({
    'GET /api/auth/me': (call) => jsonResponse(200, byUser(call)),
    'POST /api/auth/logout': () => new Response(null, { status: 204 }),
    'GET /api/base-cvs': () =>
      jsonResponse(200, {
        items: state.claimed ? [] : [guestCv({ original_filename: 'jane.pdf' })],
      }),
    'GET /api/tailoring-runs': () =>
      jsonResponse(200, { items: state.claimed ? [] : [guestRun({ id: 'guest-run-1' })] }),
    'GET /api/job-postings': () => jsonResponse(200, { items: [] }),
    'GET /api/me/base-cvs': (call) =>
      jsonResponse(200, {
        items: byUser(call) === USER_A && state.claimed ? [savedCv(A_CV)] : [],
      }),
    'GET /api/me/job-postings': (call) =>
      jsonResponse(200, {
        items:
          byUser(call) === USER_A && state.claimed
            ? [
                makePostingSummary({
                  id: 'claimed-posting',
                  title: 'A-ONLY posting',
                  expires_at: null,
                }),
              ]
            : [],
      }),
    'GET /api/me/tailoring-runs': (call) =>
      jsonResponse(
        200,
        historyPage(
          byUser(call) === USER_A && state.claimed
            ? [makeHistoryEntry({ id: 'claimed-run', base_cv_id: 'saved-1' })]
            : [],
        ),
      ),
    [`POST ${CLAIM_PATH}`]: () => {
      if (opts.hangClaim === true) {
        return new Promise<Response>((resolve) => {
          state.release = (response) => {
            state.claimed = true;
            resolve(response);
          };
        });
      }
      state.claimed = true;
      return jsonResponse(200, claimResult());
    },
  });
  return { fetch, state };
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  __resetForTests();
});

describe('Keeping guest work from the account workspace (AC-38)', () => {
  it('after Keep, the picker lists the claimed CV and the latest-run card shows the claimed run', async () => {
    const { fetch } = statefulServer();
    renderWithRouter('/', { queryClient: newClient() });
    const offer = await screen.findByRole('region', { name: OFFER_REGION_LABEL });
    expect(screen.queryByRole('radio', { name: new RegExp(A_CV) })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Your latest run' })).not.toBeInTheDocument();

    await userEvent.setup().click(within(offer).getByRole('button', { name: KEEP_LABEL }));

    expect(await screen.findByRole('radio', { name: new RegExp(A_CV) })).toBeInTheDocument();
    expect(await screen.findByRole('region', { name: 'Your latest run' })).toBeInTheDocument();
    expect(await screen.findByText(/^Kept in your account/)).toBeInTheDocument();
    const [post] = callsTo(fetch, 'POST', CLAIM_PATH);
    expect(post?.authorization).toBe(bearerFor(USER_A));
    expect(callsTo(fetch, 'POST', CLAIM_PATH)).toHaveLength(1);
  });
});

describe('Two users in one tab (AC-42)', () => {
  async function logOutAndSignInAsB(): Promise<void> {
    await userEvent.setup().click(screen.getByRole('button', { name: 'Log out' }));
    await waitFor(() => {
      expect(authStore.getSnapshot().status).toBe('anonymous');
    });
    act(() => {
      authStore.setAuthenticated({
        access_token: tokenFor(USER_B),
        token_type: 'Bearer',
        expires_in: 900,
        user: USER_B,
      });
    });
  }

  it('A claims, signs out, B signs in: B’s workspace never renders A’s claimed data', async () => {
    const { fetch } = statefulServer();
    const queryClient = newClient();
    renderWithRouter('/', { queryClient });
    const offer = await screen.findByRole('region', { name: OFFER_REGION_LABEL });
    await userEvent.setup().click(within(offer).getByRole('button', { name: KEEP_LABEL }));
    expect(await screen.findByRole('radio', { name: new RegExp(A_CV) })).toBeInTheDocument();

    let sawA = false;
    const observer = new MutationObserver(() => {
      const text = document.body.textContent;
      if (text.includes(A_CV) || text.includes('A-ONLY posting')) {
        sawA = true;
      }
    });
    await logOutAndSignInAsB();
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    try {
      await screen.findByText(/You're signed in, so what you tailor here is saved/);
      await waitFor(() => {
        const asB = callsTo(fetch, 'GET', '/api/me/base-cvs').filter(
          (call) => call.authorization === bearerFor(USER_B),
        );
        expect(asB.length).toBeGreaterThan(0);
      });
    } finally {
      observer.disconnect();
    }

    expect(sawA).toBe(false);
    expect(screen.queryByText(A_CV)).not.toBeInTheDocument();
    expect(
      queryClient.getQueryCache().findAll({ queryKey: ['auth', 'account', USER_A.id] }),
    ).toHaveLength(0);
    expect(
      queryClient.getQueryCache().findAll({ queryKey: ['auth', 'savedBaseCvs', USER_A.id] }),
    ).toHaveLength(0);
  });

  it('a claim still in flight when A signs out does not resurrect A’s data for B', async () => {
    const { state } = statefulServer({ hangClaim: true });
    const queryClient = newClient();
    renderWithRouter('/', { queryClient });
    const offer = await screen.findByRole('region', { name: OFFER_REGION_LABEL });
    await userEvent.setup().click(within(offer).getByRole('button', { name: KEEP_LABEL }));
    await within(offer).findByRole('button', { name: KEEP_PENDING_LABEL });

    await logOutAndSignInAsB();
    await act(async () => {
      state.release(jsonResponse(200, claimResult()));
      await Promise.resolve();
    });
    await screen.findByText(/You're signed in, so what you tailor here is saved/);

    expect(screen.queryByText(A_CV)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Kept in your account/)).not.toBeInTheDocument();
    expect(
      queryClient.getQueryCache().findAll({ queryKey: ['auth', 'savedBaseCvs', USER_A.id] }),
    ).toHaveLength(0);
    expect(
      queryClient.getQueryCache().findAll({ queryKey: ['auth', 'account', USER_A.id] }),
    ).toHaveLength(0);
  });
});
