import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DEMAND_HEADING, ERASE_LABEL, eraseDialogMessage } from '@/features/admin/adminCopy';
import { __resetForTests, authStore } from '@/features/auth/authStore';
import { currentUserQueryKey } from '@/features/auth/hooks/authCache';
import { DeleteAccountSection } from '@/features/auth/components/DeleteAccountSection';
import { COMPARE_HEADING } from '@/features/scoring/fitScoreCopy';
import { createAppQueryClient } from '@/queryClient';
import {
  USER_A,
  callsTo,
  hang,
  makeAccountRun,
  ok,
  signInAs,
  signedInRoutes,
  status,
  stubAccountFetch,
} from '@/test/accountFetch';
import { COUNTS, GUEST_HOME, adminOnly, detail, noContent, page } from '@/test/adminFetch';
import { contentRoutes, world } from '@/test/adminContentFetch';
import { guestRoutes, guestWorld } from '@/test/adminGuestsFetch';
import {
  jsonResponse,
  makeExtractedCv,
  makePostingSummary,
  makeRun,
  stubWorkspaceFetch,
} from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

import {
  FAILED_RUN_REFUND,
  GUEST_OFFER_RUN,
  GUEST_OFFER_WORKSPACE,
  OUT_OF_CREDITS_HEADING,
  OUT_OF_CREDITS_KEEP,
  UNUSED_CREDITS_FALLBACK,
  unusedCreditsWarning,
} from './billingCopy';

import type { RouteHandler } from '@/test/accountFetch';
import type { User } from '@/features/auth/types';
import type { SavedBaseCv } from '@/features/savedCvs/types';

/**
 * T37 (`qa`, test-after; AC-32, AC-33, AC-36, AC-37, AC-39): slice 5.2's **wiring**, through the
 * real route table. The components' own states are `billing.states.test.tsx`; here is what the
 * pages do with them. Every absence has a positive control (the read that would have caused the
 * presence has settled, or the same page shows the thing in the sibling state).
 */

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// --- Fixtures ----------------------------------------------------------------------------------

const PACKS = [
  { id: 'pack_3', credits: 3, price_cents: 500 },
  { id: 'pack_10', credits: 10, price_cents: 1200 },
] as const;

function creditsBody(balance: number, overrides: object = {}): object {
  return {
    balance,
    out_of_credits: balance === 0,
    notice: null,
    payment_notice_opted_in: false,
    packs: PACKS,
    entries: [],
    ...overrides,
  };
}

const credits = (balance: number, overrides: object = {}): RouteHandler =>
  ok(creditsBody(balance, overrides));

const SAVED_CV: SavedBaseCv = {
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

function accountWorkspace(overrides: Record<string, RouteHandler> = {}) {
  signInAs(USER_A);
  return stubAccountFetch({
    ...signedInRoutes(USER_A),
    'GET /api/me/base-cvs': ok({ items: [SAVED_CV] }),
    'GET /api/me/job-postings': ok({
      items: [makePostingSummary({ id: 'p-1', title: 'Platform Engineer', expires_at: null })],
    }),
    'GET /api/me/tailoring-runs': ok({ items: [], next_cursor: null }),
    'GET /api/me/credits': credits(3),
    ...overrides,
  });
}

const TAILOR = { name: /^tailor/i };
const mainOf = (): HTMLElement => screen.getByRole('main');
const h1sIn = (el: HTMLElement): HTMLElement[] =>
  within(el).queryAllByRole('heading', { level: 1 });

async function tailorButton(): Promise<HTMLElement> {
  const button = await screen.findByRole('button', TAILOR);
  await waitFor(() => {
    expect(button).toBeEnabled();
  });
  return button;
}

// --- The account workspace (AC-33) ---------------------------------------------------------------

describe('the account workspace and credits (AC-33)', () => {
  it('shows OutOfCredits, with the packs, and no Tailor button when the read says out_of_credits', async () => {
    accountWorkspace({ 'GET /api/me/credits': credits(0) });
    renderWithRouter('/', { queryClient: createAppQueryClient() });

    expect(await screen.findByRole('heading', { name: OUT_OF_CREDITS_HEADING })).toBeVisible();
    expect(screen.getByText(OUT_OF_CREDITS_KEEP)).toBeVisible();
    expect(screen.getByRole('button', { name: /3 credits for \$5/ })).toBeVisible();
    expect(screen.queryByRole('button', TAILOR)).not.toBeInTheDocument();
  });

  it('with a balance, shows Tailor and the cost line, and not OutOfCredits (positive control)', async () => {
    accountWorkspace();
    renderWithRouter('/', { queryClient: createAppQueryClient() });

    expect(await tailorButton()).toBeEnabled();
    expect(await screen.findByText('Uses 1 credit · You have 3')).toBeVisible();
    expect(screen.queryByRole('heading', { name: OUT_OF_CREDITS_HEADING })).not.toBeInTheDocument();
  });

  it('a credits read still loading swaps nothing: Tailor is enabled', async () => {
    const fetch = accountWorkspace({ 'GET /api/me/credits': hang });
    renderWithRouter('/', { queryClient: createAppQueryClient() });

    expect(await tailorButton()).toBeEnabled();
    await waitFor(() => {
      expect(callsTo(fetch, 'GET', '/api/me/credits').length).toBeGreaterThan(0);
    });
    expect(screen.queryByRole('heading', { name: OUT_OF_CREDITS_HEADING })).not.toBeInTheDocument();
  });

  it('a credits read that failed swaps nothing: Tailor is enabled', async () => {
    const fetch = accountWorkspace({
      'GET /api/me/credits': status(400, 'bad_request', 'fixture'),
    });
    renderWithRouter('/', { queryClient: createAppQueryClient() });

    expect(await screen.findByText("Couldn't load your credits.")).toBeVisible();
    expect(callsTo(fetch, 'GET', '/api/me/credits')).toHaveLength(1);
    expect(await tailorButton()).toBeEnabled();
    expect(screen.queryByRole('heading', { name: OUT_OF_CREDITS_HEADING })).not.toBeInTheDocument();
  });

  /** Credits answer 3 once, then hang: only the 409 can explain a swap that follows the click. */
  function refusedWorld(laterRead: RouteHandler): Record<string, RouteHandler> {
    return {
      'GET /api/me/credits': (call, n) => (n === 1 ? credits(3)(call, n) : laterRead(call, n)),
      'POST /api/me/tailoring-runs': status(409, 'no_credits', 'No credits left.'),
    };
  }

  it('a 409 no_credits swaps in OutOfCredits, with no rejection notice', async () => {
    accountWorkspace(refusedWorld(hang));
    renderWithRouter('/', { queryClient: createAppQueryClient() });

    await userEvent.setup().click(await tailorButton());

    expect(await screen.findByRole('heading', { name: OUT_OF_CREDITS_HEADING })).toBeVisible();
    expect(screen.queryByRole('button', TAILOR)).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText('No credits left.')).not.toBeInTheDocument();
  });

  it('the swap holds while the re-read is pending and ends when a later read answers with a balance', async () => {
    accountWorkspace(refusedWorld(credits(2)));
    renderWithRouter('/', { queryClient: createAppQueryClient() });

    await userEvent.setup().click(await tailorButton());

    // The re-read says balance 2: the server's own word wins over the earlier refusal.
    await waitFor(() => {
      expect(
        screen.queryByRole('heading', { name: OUT_OF_CREDITS_HEADING }),
      ).not.toBeInTheDocument();
    });
    expect(await tailorButton()).toBeEnabled();
  });

  it('a re-read in the same millisecond as the click still ends the swap', async () => {
    // CI ran the click, the 409 and the re-read inside one millisecond: a strict `>` against
    // `submittedAt` never counted that read, and the swap held forever. Freeze the clock to pin it.
    vi.spyOn(Date, 'now').mockReturnValue(1_700_000_000_000);
    accountWorkspace(refusedWorld(credits(2)));
    renderWithRouter('/', { queryClient: createAppQueryClient() });

    await userEvent.setup().click(await tailorButton());

    await waitFor(() => {
      expect(
        screen.queryByRole('heading', { name: OUT_OF_CREDITS_HEADING }),
      ).not.toBeInTheDocument();
    });
    expect(await tailorButton()).toBeEnabled();
  });

  it('has no <h1> of its own in main (the header owns the page h1)', async () => {
    accountWorkspace();
    renderWithRouter('/', { queryClient: createAppQueryClient() });
    await tailorButton();

    expect(h1sIn(mainOf())).toHaveLength(0);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  });

  it('at 360 px the out-of-credits packs wrap (markup: jsdom has no layout)', async () => {
    accountWorkspace({ 'GET /api/me/credits': credits(0) });
    renderWithRouter('/', { queryClient: createAppQueryClient() });

    const pack = await screen.findByRole('button', { name: /3 credits for \$5/ });
    expect(pack.parentElement?.className).toContain('flex-wrap');
    expect(pack.className).toContain('min-h-11');
  });
});

// --- The guest workspace (AC-36) -----------------------------------------------------------------

describe('the guest workspace (AC-36)', () => {
  beforeEach(() => {
    authStore.signOut('expired');
  });

  it('shows the offer while the workspace loads, and no Tailor button', async () => {
    stubWorkspaceFetch({ baseCvs: () => new Promise<Response>(() => undefined) });
    renderWithRouter('/');

    expect(await screen.findByText(GUEST_OFFER_WORKSPACE)).toBeVisible();
    expect(screen.getByRole('link', { name: 'Create free account' })).toHaveAttribute(
      'href',
      '/register',
    );
    expect(screen.queryByRole('button', TAILOR)).not.toBeInTheDocument();
  });

  it('keeps the offer, and still no Tailor button, once the lists have loaded', async () => {
    stubWorkspaceFetch({
      baseCvs: () => Promise.resolve(jsonResponse(200, { items: [makeExtractedCv()] })),
      jobPostings: () => Promise.resolve(jsonResponse(200, { items: [makePostingSummary()] })),
      runsList: () => Promise.resolve(jsonResponse(200, { items: [] })),
    });
    renderWithRouter('/');

    await waitFor(() => {
      expect(screen.queryByText('Loading…')).not.toBeInTheDocument();
    });
    expect(screen.getByText(GUEST_OFFER_WORKSPACE)).toBeVisible();
    expect(screen.queryByRole('button', TAILOR)).not.toBeInTheDocument();
  });
});

// --- The run pages (AC-33, AC-36, AC-37) ---------------------------------------------------------

const RUN_ID = 'run-credits-wiring';

function accountRunPage(run: object, extra: Record<string, RouteHandler> = {}) {
  signInAs(USER_A);
  stubAccountFetch({
    ...signedInRoutes(USER_A),
    'GET /api/me/tailoring-runs/:id': ok(run),
    'GET /api/me/tailoring-runs/:id/exports': ok({ items: [] }),
    'GET /api/me/tailoring-runs/:id/fit-scores': hang,
    ...extra,
  });
  return renderWithRouter(`/history/${RUN_ID}/cv`, { queryClient: createAppQueryClient() });
}

function guestRunPage(run: object) {
  authStore.signOut('expired');
  stubWorkspaceFetch({ runDetail: { [RUN_ID]: () => Promise.resolve(jsonResponse(200, run)) } });
  return renderWithRouter(`/runs/${RUN_ID}/cv`);
}

const SUCCEEDED = {
  id: RUN_ID,
  status: 'succeeded' as const,
  tailored_cv: 'my tailored cv',
  cover_letter: 'my cover letter',
  tailored_cv_character_count: 14,
  cover_letter_character_count: 15,
};
const FAILED = {
  id: RUN_ID,
  status: 'failed' as const,
  failure_reason: 'llm_timed_out' as const,
  retryable: true,
};

describe('the run pages (AC-33, AC-36, AC-37)', () => {
  it("an account's failed run says failed runs don't use a credit, and keeps Try again", async () => {
    accountRunPage(makeAccountRun(FAILED));

    expect(await screen.findByText(FAILED_RUN_REFUND)).toBeVisible();
    expect(screen.getByRole('button', { name: /try again/i })).toBeVisible();
  });

  it("a guest's failed run has no Try again and no credit sentence", async () => {
    guestRunPage(makeRun(FAILED));

    expect(await screen.findByText('That took too long.')).toBeVisible();
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument();
    expect(screen.queryByText(FAILED_RUN_REFUND)).not.toBeInTheDocument();
  });

  it("an account's succeeded run shows the comparison panel and the export bar", async () => {
    accountRunPage(makeAccountRun(SUCCEEDED));

    expect(await screen.findByRole('heading', { name: COMPARE_HEADING })).toBeVisible();
    expect(await screen.findByRole('radio', { name: /Formal/ })).toBeVisible();
    expect(screen.queryByText(GUEST_OFFER_RUN)).not.toBeInTheDocument();
  });

  it("an account's failed run has no comparison panel (positive control: the failure rendered)", async () => {
    accountRunPage(makeAccountRun(FAILED));

    await screen.findByText(FAILED_RUN_REFUND);
    expect(screen.queryByRole('heading', { name: COMPARE_HEADING })).not.toBeInTheDocument();
  });

  it("an account's running run has no comparison panel (positive control: it is working)", async () => {
    accountRunPage(makeAccountRun({ id: RUN_ID, status: 'running' }));

    await screen.findByText(/Tailoring with Gemini/);
    expect(screen.queryByRole('heading', { name: COMPARE_HEADING })).not.toBeInTheDocument();
  });

  it("a guest's succeeded run shows the offer where the export bar was, with no comparison", async () => {
    guestRunPage(makeRun(SUCCEEDED));

    expect(await screen.findByText(GUEST_OFFER_RUN)).toBeVisible();
    expect(screen.getByRole('link', { name: 'Create free account' })).toHaveAttribute(
      'href',
      '/register',
    );
    expect(screen.queryByRole('radio', { name: /Formal/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: COMPARE_HEADING })).not.toBeInTheDocument();
  });

  it.each([
    ['an account run', () => accountRunPage(makeAccountRun(SUCCEEDED))],
    ['a guest run', () => guestRunPage(makeRun(SUCCEEDED))],
  ])('%s page has no <h1> inside main', async (_name, mount) => {
    mount();
    await screen.findByRole('heading', { name: 'Your tailored CV and cover letter' });

    expect(h1sIn(mainOf())).toHaveLength(0);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  });

  it('the guest offer at 360 px: the sign-up link is full width, the pair wraps', async () => {
    guestRunPage(makeRun(SUCCEEDED));

    const signUp = await screen.findByRole('link', { name: 'Create free account' });
    expect(signUp.className).toContain('max-sm:w-full');
    expect(signUp.parentElement?.className).toContain('flex-wrap');
  });
});

// --- The account page and the delete warning (AC-32) ---------------------------------------------

function mountDeleteSection(table: Record<string, RouteHandler>, knownUser: boolean) {
  const fetch = stubAccountFetch(table);
  signInAs(USER_A);
  const queryClient = createAppQueryClient();
  if (knownUser) queryClient.setQueryData(currentUserQueryKey, USER_A);
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <DeleteAccountSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return fetch;
}

describe('the delete-account warning (AC-32)', () => {
  it('names the unused credits when the balance is above 0', async () => {
    mountDeleteSection({ 'GET /api/me/credits': credits(2) }, true);

    expect(await screen.findByText(unusedCreditsWarning(2))).toBeVisible();
    expect(screen.queryByText(UNUSED_CREDITS_FALLBACK)).not.toBeInTheDocument();
  });

  it('falls back to the plain sentence when the credits cannot be read', async () => {
    const fetch = mountDeleteSection(
      { 'GET /api/me/credits': status(400, 'bad_request', 'fixture') },
      true,
    );

    await waitFor(() => {
      expect(callsTo(fetch, 'GET', '/api/me/credits')).toHaveLength(1);
    });
    expect(await screen.findByText(UNUSED_CREDITS_FALLBACK)).toBeVisible();
    expect(screen.queryByText(/\d+ unused credit/)).not.toBeInTheDocument();
  });

  it('falls back when /me is not readable, without asking for credits', async () => {
    const fetch = mountDeleteSection(
      { 'GET /api/auth/me': hang, 'GET /api/me/credits': credits(2) },
      false,
    );

    expect(await screen.findByText(UNUSED_CREDITS_FALLBACK)).toBeVisible();
    expect(callsTo(fetch, 'GET', '/api/me/credits')).toHaveLength(0);
    expect(screen.queryByText(/\d+ unused credit/)).not.toBeInTheDocument();
  });

  it('shows nothing about credits at balance 0 (positive control: the read settled)', async () => {
    const fetch = mountDeleteSection({ 'GET /api/me/credits': credits(0) }, true);

    await waitFor(() => {
      expect(screen.queryByText(UNUSED_CREDITS_FALLBACK)).not.toBeInTheDocument();
    });
    expect(callsTo(fetch, 'GET', '/api/me/credits')).toHaveLength(1);
    expect(screen.queryByText(/\d+ unused credit/)).not.toBeInTheDocument();
  });
});

describe('the account page', () => {
  it('lists the credits section and keeps the delete warning, with one <h1> on the page', async () => {
    signInAs(USER_A);
    stubAccountFetch({
      ...signedInRoutes(USER_A),
      'GET /api/me/credits': credits(2),
      'GET /api/me/base-cvs': ok({ items: [] }),
    });
    renderWithRouter('/account', { queryClient: createAppQueryClient() });

    expect(await screen.findByRole('heading', { name: 'Credits' })).toBeVisible();
    expect(await screen.findByText('You have 2 credits')).toBeVisible();
    expect(await screen.findByText(unusedCreditsWarning(2))).toBeVisible();
    expect(h1sIn(mainOf())).toHaveLength(0);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  });
});

// --- /admin (AC-32, AC-39) -----------------------------------------------------------------------

const ADMIN: User = { ...USER_A, id: 'admin-1', email: 'root@example.com', role: 'admin' };

function adminUserPage(creditsLeft: number) {
  const ann = detail({
    id: 'u-ann',
    email: 'ann@example.com',
    counts: { ...COUNTS, credits_left: creditsLeft },
  });
  stubAccountFetch({
    ...signedInRoutes(ADMIN),
    ...GUEST_HOME,
    'GET /api/admin/access': noContent,
    'GET /api/admin/users': adminOnly(ADMIN, page([ann])),
    'GET /api/admin/users/:id': adminOnly(ADMIN, () => Response.json(ann)),
    'GET /api/admin/users/:id/base-cvs': adminOnly(ADMIN, ok({ items: [] })),
    'GET /api/admin/users/:id/job-postings': adminOnly(ADMIN, ok({ items: [] })),
    'GET /api/admin/users/:id/tailoring-runs': adminOnly(
      ADMIN,
      ok({ items: [], next_cursor: null }),
    ),
  });
  signInAs(ADMIN);
  renderWithRouter('/admin/users/u-ann', { queryClient: createAppQueryClient() });
  return ann;
}

async function openEraseDialog(): Promise<HTMLElement> {
  const control = await screen.findByRole('button', { name: ERASE_LABEL });
  await waitFor(() => {
    expect(control).toBeEnabled();
  });
  await userEvent.setup().click(control);
  return screen.findByRole('alertdialog');
}

describe('the admin erase dialog (AC-32)', () => {
  it('appends the unused-credits warning when credits_left is above 0', async () => {
    const ann = adminUserPage(4);

    const dialog = await openEraseDialog();

    expect(dialog).toHaveTextContent(unusedCreditsWarning(4));
    expect(dialog).toHaveTextContent(eraseDialogMessage(ann.email, ann.counts, false));
  });

  it('says nothing about credits at 0 (positive control: the dialog opened and counts show)', async () => {
    adminUserPage(0);

    const dialog = await openEraseDialog();

    expect(dialog).toHaveTextContent('This cannot be undone.');
    expect(dialog).not.toHaveTextContent(/unused credit/);
  });
});

describe('the admin Demand page (AC-39)', () => {
  function adminAt(path: string, demand: RouteHandler) {
    stubAccountFetch({
      ...contentRoutes(world()),
      ...guestRoutes(guestWorld()),
      'GET /api/admin/demand-counters': adminOnly(ADMIN, demand),
    });
    signInAs(ADMIN);
    renderWithRouter(path, { queryClient: createAppQueryClient() });
  }

  it('the Demand nav item is the current area on /admin/demand, and exactly one', async () => {
    adminAt('/admin/demand', hang);

    const link = await screen.findByRole('link', { name: 'Demand' });
    const nav = link.closest('nav');
    if (nav === null) throw new Error('the Demand link is not inside a nav');
    const current = within(nav)
      .getAllByRole('link')
      .filter((a) => a.getAttribute('aria-current') === 'page')
      .map((a) => a.textContent);
    expect(current).toEqual(['Demand']);
    expect(link).toHaveAttribute('href', '/admin/demand');
  });

  it('has one <h1> inside main, named Demand', async () => {
    adminAt('/admin/demand', hang);

    const h1 = await screen.findByRole('heading', { level: 1, name: DEMAND_HEADING });
    expect(h1sIn(mainOf())).toEqual([h1]);
  });

  it('labels its table and scrolls it sideways rather than the page at 360 px', async () => {
    const counts = {
      fit_scores: 1,
      fit_scores_busy: 0,
      sign_ups: 2,
      pack_clicks: 3,
      pack_clickers: 1,
      runs_failed: 0,
      runs_succeeded: 4,
    };
    adminAt('/admin/demand?from=2026-11-01&to=2026-11-01', () =>
      Response.json({
        from: '2026-11-01',
        to: '2026-11-01',
        days: [{ day: '2026-11-01', counts }],
        totals: counts,
      }),
    );

    const table = await screen.findByRole('table', { name: /Daily counts/ });
    expect(table.parentElement?.className).toContain('overflow-x-auto');
    expect(within(table).getByRole('row', { name: /^Total/ })).toBeVisible();
    expect(table.closest('section')?.className).toContain('min-w-0');
  });
});
