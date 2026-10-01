import { QueryClient } from '@tanstack/react-query';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import {
  USER_A,
  callsTo,
  makeAccountRun,
  ok,
  signInAs,
  status,
  stubAccountFetch,
} from '@/test/accountFetch';
import { jsonResponse, makeRun } from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

import {
  CTA_LOGIN_LABEL,
  CTA_REGION_LABEL,
  CTA_REGISTER_LABEL,
  KEEP_LABEL,
  NOT_NOW_LABEL,
  OFFER_REGION_LABEL,
  SESSION_ENDED_HISTORY_LINK_LABEL,
  SESSION_ENDED_HISTORY_NOTE,
} from '../claimCopy';
import { CLAIM_PATH, claimResult, guestCv, guestListRoutes, guestRun } from '../test/support';

import type { RouteHandler } from '@/test/accountFetch';

/**
 * T30 RED — the slot on a guest run page (AC-35, AC-38's navigation, AC-41; C-34…C-36, C-42, C-43),
 * mounted through the REAL route table at `/runs/:id/:document`, because what is under test is the
 * wiring as much as the component: `RunPage` renders `GuestRunPrompt` in the guest scope only, and
 * T29 left both unwired.
 *
 * **Absence is paired.** A skeleton renders nothing, so every "no CTA / no offer" test first waits
 * for the page itself to have rendered the thing it is in the state of (the document, the stepper's
 * sentence), and the tests that show the region are its positive control.
 */

const RUN_ID = 'run-1';
const CV_TEXT = 'my tailored cv text';
const COVER_TEXT = 'my cover letter text';
const EXPIRED_COPY = 'Your session has expired. Upload your CV again.';

function guestRunDetail(overrides: Parameters<typeof makeRun>[0] = {}) {
  return makeRun({
    id: RUN_ID,
    status: 'succeeded',
    tailored_cv: CV_TEXT,
    cover_letter: COVER_TEXT,
    tailored_cv_character_count: CV_TEXT.length,
    cover_letter_character_count: COVER_TEXT.length,
    completed_at: '2026-09-12T10:00:09Z',
    ...overrides,
  });
}

function pageRoutes(
  run: ReturnType<typeof makeRun> | RouteHandler,
  extra: Record<string, RouteHandler> = {},
): Record<string, RouteHandler> {
  return {
    ...guestListRoutes([], []),
    'GET /api/tailoring-runs/:id': typeof run === 'function' ? run : ok(run),
    'GET /api/tailoring-runs/:id/exports': ok({ items: [] }),
    ...extra,
  };
}

function anonymous(): void {
  __resetForTests();
  authStore.signOut('expired');
}

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  __resetForTests();
});

// --- AC-35 / C-34 / C-35 / C-43: the CTA ------------------------------------------------------------

describe('Registration CTA on a guest run page (AC-35)', () => {
  it('C-34: anonymous on a succeeded run sees the region with both links carrying this page as next', async () => {
    stubAccountFetch(pageRoutes(guestRunDetail()));
    anonymous();

    renderWithRouter(`/runs/${RUN_ID}/cv`);

    const region = await screen.findByRole('region', { name: CTA_REGION_LABEL });
    expect(within(region).getByRole('link', { name: CTA_REGISTER_LABEL })).toHaveAttribute(
      'href',
      `/register?next=%2Fruns%2F${RUN_ID}%2Fcv`,
    );
    expect(within(region).getByRole('link', { name: CTA_LOGIN_LABEL })).toHaveAttribute(
      'href',
      `/login?next=%2Fruns%2F${RUN_ID}%2Fcv`,
    );
    expect(screen.queryByRole('region', { name: OFFER_REGION_LABEL })).not.toBeInTheDocument();
  });

  it('carries the document the visitor is on, so they come back to the same tab', async () => {
    stubAccountFetch(pageRoutes(guestRunDetail()));
    anonymous();

    renderWithRouter(`/runs/${RUN_ID}/cover_letter`);

    const region = await screen.findByRole('region', { name: CTA_REGION_LABEL });
    expect(within(region).getByRole('link', { name: CTA_REGISTER_LABEL })).toHaveAttribute(
      'href',
      `/register?next=%2Fruns%2F${RUN_ID}%2Fcover_letter`,
    );
  });

  it('Not now hides it for this page and nothing is requested', async () => {
    const fetch = stubAccountFetch(pageRoutes(guestRunDetail()));
    anonymous();
    renderWithRouter(`/runs/${RUN_ID}/cv`);
    const region = await screen.findByRole('region', { name: CTA_REGION_LABEL });

    await userEvent.setup().click(within(region).getByRole('button', { name: NOT_NOW_LABEL }));

    expect(screen.queryByRole('region', { name: CTA_REGION_LABEL })).not.toBeInTheDocument();
    expect(fetch.calls.filter((call) => call.method !== 'GET')).toHaveLength(0);
  });

  it.each([
    ['queued', 'Waiting for a worker…'],
    ['running', /Tailoring with Gemini/],
  ] as const)(
    'C-35: no CTA while the run is %s (its first job is the progress, not a pitch)',
    async (runStatus, sentence) => {
      stubAccountFetch(pageRoutes(guestRunDetail({ status: runStatus })));
      anonymous();

      renderWithRouter(`/runs/${RUN_ID}/cv`);

      expect(await screen.findByText(sentence)).toBeInTheDocument();
      expect(screen.queryByRole('region', { name: CTA_REGION_LABEL })).not.toBeInTheDocument();
    },
  );

  it('C-35: no CTA on a failed run', async () => {
    stubAccountFetch(
      pageRoutes(
        guestRunDetail({
          status: 'failed',
          failure_reason: 'llm_timed_out',
          retryable: true,
          tailored_cv: null,
          cover_letter: null,
        }),
      ),
    );
    anonymous();

    renderWithRouter(`/runs/${RUN_ID}/cv`);

    expect(await screen.findByText('That took too long.')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: CTA_REGION_LABEL })).not.toBeInTheDocument();
  });

  it('C-43: no CTA and no offer while auth is booting — "who is this?" has no answer yet', async () => {
    stubAccountFetch(pageRoutes(guestRunDetail()));
    // `booting`: the store after `__resetForTests`, never bootstrapped.

    renderWithRouter(`/runs/${RUN_ID}/cv`);

    expect(await screen.findByText(CV_TEXT)).toBeInTheDocument();
    expect(authStore.getSnapshot().status).toBe('booting');
    expect(screen.queryByRole('region', { name: CTA_REGION_LABEL })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: OFFER_REGION_LABEL })).not.toBeInTheDocument();
  });

  it('C-43: no CTA and no offer while auth is unavailable', async () => {
    stubAccountFetch(
      pageRoutes(guestRunDetail(), {
        'POST /api/auth/refresh': status(503, 'service_unavailable'),
      }),
    );
    await authStore.bootstrap();
    expect(authStore.getSnapshot().status).toBe('unavailable');

    renderWithRouter(`/runs/${RUN_ID}/cv`);

    expect(await screen.findByText(CV_TEXT)).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: CTA_REGION_LABEL })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: OFFER_REGION_LABEL })).not.toBeInTheDocument();
  });

  it('never in account scope: a history run, signed in, shows neither the CTA nor the offer', async () => {
    signInAs(USER_A);
    stubAccountFetch({
      ...guestListRoutes([guestCv()], [guestRun()]),
      'GET /api/me/tailoring-runs/:id': ok(
        makeAccountRun({
          id: RUN_ID,
          status: 'succeeded',
          tailored_cv: CV_TEXT,
          cover_letter: COVER_TEXT,
          tailored_cv_character_count: 19,
          cover_letter_character_count: 20,
        }),
      ),
      'GET /api/me/tailoring-runs/:id/exports': ok({ items: [] }),
    });

    renderWithRouter(`/history/${RUN_ID}/cv`);

    expect(await screen.findByText(CV_TEXT)).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: CTA_REGION_LABEL })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: OFFER_REGION_LABEL })).not.toBeInTheDocument();
  });
});

// --- C-36 / AC-38: authenticated on a guest run ------------------------------------------------------

describe('Claim offer on a guest run page (AC-37, AC-38, C-36)', () => {
  it('C-36: signed in with guest work present, the page shows the offer, not the CTA', async () => {
    signInAs(USER_A);
    stubAccountFetch(
      pageRoutes(guestRunDetail(), {
        ...guestListRoutes(
          [guestCv({ original_filename: 'jane.pdf' })],
          [guestRun({ id: RUN_ID })],
        ),
      }),
    );

    renderWithRouter(`/runs/${RUN_ID}/cv`);

    const offer = await screen.findByRole('region', { name: OFFER_REGION_LABEL });
    expect(offer).toHaveTextContent('jane.pdf');
    expect(screen.queryByRole('region', { name: CTA_REGION_LABEL })).not.toBeInTheDocument();
  });

  it('C-37: signed in with nothing to keep, the page shows neither', async () => {
    signInAs(USER_A);
    const fetch = stubAccountFetch(pageRoutes(guestRunDetail()));

    renderWithRouter(`/runs/${RUN_ID}/cv`);

    expect(await screen.findByText(CV_TEXT)).toBeInTheDocument();
    await waitFor(() => {
      expect(callsTo(fetch, 'GET', '/api/base-cvs').length).toBeGreaterThan(0);
    });
    expect(screen.queryByRole('region', { name: OFFER_REGION_LABEL })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: CTA_REGION_LABEL })).not.toBeInTheDocument();
  });

  it.each(['cv', 'cover_letter'] as const)(
    'Keep on /runs/:id/%s REPLACES the entry with /history/:id/%s, after the claim answered',
    async (documentSegment) => {
      signInAs(USER_A);
      const fetch = stubAccountFetch(
        pageRoutes(guestRunDetail(), {
          ...guestListRoutes([guestCv()], [guestRun({ id: RUN_ID })]),
          [`POST ${CLAIM_PATH}`]: ok(claimResult()),
          'GET /api/me/tailoring-runs/:id': ok(
            makeAccountRun({
              id: RUN_ID,
              status: 'succeeded',
              tailored_cv: CV_TEXT,
              cover_letter: COVER_TEXT,
              tailored_cv_character_count: 19,
              cover_letter_character_count: 20,
            }),
          ),
          'GET /api/me/tailoring-runs/:id/exports': ok({ items: [] }),
        }),
      );
      const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false, gcTime: Infinity } },
      });
      const { router } = renderWithRouter(`/runs/${RUN_ID}/${documentSegment}`, { queryClient });
      const offer = await screen.findByRole('region', { name: OFFER_REGION_LABEL });
      expect(router.state.location.pathname).toBe(`/runs/${RUN_ID}/${documentSegment}`);

      await userEvent.setup().click(within(offer).getByRole('button', { name: KEEP_LABEL }));

      await waitFor(() => {
        expect(router.state.location.pathname).toBe(`/history/${RUN_ID}/${documentSegment}`);
      });
      expect(router.state.historyAction).toBe('REPLACE');
      expect(callsTo(fetch, 'POST', CLAIM_PATH)).toHaveLength(1);
      // The guest session no longer exists: nothing of it stays cached.
      expect(queryClient.getQueryCache().findAll({ queryKey: ['tailoring'] })).toHaveLength(0);
      // …and the page the visitor lands on is the account's run, read with /api/me/.
      await waitFor(() => {
        expect(callsTo(fetch, 'GET', '/api/me/tailoring-runs/:id').length).toBeGreaterThan(0);
      });
    },
  );

  it('a claim that fails leaves the visitor on the guest run, with the alert', async () => {
    signInAs(USER_A);
    stubAccountFetch(
      pageRoutes(guestRunDetail(), {
        ...guestListRoutes([guestCv()], [guestRun({ id: RUN_ID })]),
        [`POST ${CLAIM_PATH}`]: status(503, 'service_unavailable'),
      }),
    );
    const { router } = renderWithRouter(`/runs/${RUN_ID}/cv`);
    const offer = await screen.findByRole('region', { name: OFFER_REGION_LABEL });

    await userEvent.setup().click(within(offer).getByRole('button', { name: KEEP_LABEL }));

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe(`/runs/${RUN_ID}/cv`);
  });
});

// --- AC-41 / C-42: a guest run page whose session has ended -----------------------------------------------

describe('A guest run page whose session has ended (AC-41, C-42)', () => {
  const expired = (): Response =>
    jsonResponse(401, { error: { code: 'guest_session_expired', message: 'expired' } });

  it('signed in: 1.4’s copy gains the history note and a link to /history/:id/:document', async () => {
    signInAs(USER_A);
    stubAccountFetch({
      ...guestListRoutes([], []),
      'GET /api/tailoring-runs/:id': expired,
    });

    renderWithRouter(`/runs/${RUN_ID}/cover_letter`);

    expect(await screen.findByText(EXPIRED_COPY)).toBeInTheDocument();
    expect(screen.getByText(SESSION_ENDED_HISTORY_NOTE)).toBeInTheDocument();
    expect(SESSION_ENDED_HISTORY_NOTE).toBe(
      "If you kept this work in your account, it's in your history.",
    );
    expect(screen.getByRole('link', { name: SESSION_ENDED_HISTORY_LINK_LABEL })).toHaveAttribute(
      'href',
      `/history/${RUN_ID}/cover_letter`,
    );
  });

  it('anonymous: 1.4’s copy, unchanged — no note and no history link', async () => {
    stubAccountFetch({
      ...guestListRoutes([], []),
      'GET /api/tailoring-runs/:id': expired,
    });
    anonymous();

    renderWithRouter(`/runs/${RUN_ID}/cv`);

    expect(await screen.findByText(EXPIRED_COPY)).toBeInTheDocument();
    expect(screen.queryByText(SESSION_ENDED_HISTORY_NOTE)).not.toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: SESSION_ENDED_HISTORY_LINK_LABEL }),
    ).not.toBeInTheDocument();
  });
});
