import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { currentUserQueryKey } from '@/features/auth/hooks/authCache';
import { __resetForTests } from '@/features/auth/authStore';
import { WorkspaceScopeProvider } from '@/features/scope/WorkspaceScope';
import { createAppQueryClient } from '@/queryClient';
import {
  USER_A,
  callsTo,
  hang,
  ok,
  signInAs,
  status,
  stubAccountFetch,
  type AccountFetch,
  type RouteHandler,
} from '@/test/accountFetch';
import { jsonResponse } from '@/test/fixtures';

import {
  CLOSE,
  CREDITS_HEADING,
  CREDITS_LOAD_ERROR,
  CREDITS_LOADING,
  CREDITS_NOTICE,
  ENTRY_LABELS,
  GUEST_OFFER_HEADING,
  GUEST_OFFER_RUN,
  GUEST_OFFER_SIGN_IN,
  GUEST_OFFER_SIGN_UP,
  GUEST_OFFER_WORKSPACE,
  NO_ENTRIES,
  OPT_IN_ERROR,
  OPT_IN_SAVING,
  OUT_OF_CREDITS_HEADING,
  OUT_OF_CREDITS_KEEP,
  PACK_CLICK_ERROR,
  PACK_DIALOG_HEADING,
  PACK_OPENING_SOON,
  PACK_PENDING,
  RETRY,
  balanceLine,
  creditsLine,
  optInLabel,
  packLabel,
  signedCredits,
} from '../billingCopy';
import { creditsQueryKey } from '../hooks/billingKeys';
import { CreditsLine } from './CreditsLine';
import { CreditsNotice } from './CreditsNotice';
import { CreditsSection } from './CreditsSection';
import { GuestTailorOffer } from './GuestTailorOffer';
import { OutOfCredits } from './OutOfCredits';
import { PackDialog } from './PackDialog';
import { PackOffer } from './PackOffer';

import type { Credits, PackTerms } from '../types';

/**
 * T33 RED (slice 5.2; AC-33 to AC-36, technical plan §5's table): every billing component through a
 * fake server whose bodies have `schemas/billing.py`'s shape, on the app's own client. A credits
 * read is failed with a 4xx (an answer, not retried) so the error state needs no backoff wait.
 * Absences have a positive control (the read settled first). Whether Tailor stays enabled, and the
 * 409 swap itself, are wiring (T35); the 409's invalidation is guarded in `billing.hooks.test.tsx`.
 */

const PACK_3: PackTerms = { id: 'pack_3', credits: 3, price_cents: 500 };
const PACK_10: PackTerms = { id: 'pack_10', credits: 10, price_cents: 1200 };

function credits(overrides: Partial<Credits> = {}): Credits {
  return {
    balance: 3,
    out_of_credits: false,
    notice: null,
    payment_notice_opted_in: false,
    packs: [PACK_3, PACK_10],
    entries: [],
    ...overrides,
  };
}

interface World {
  credits: Credits;
}

function routes(
  world: World,
  extra: Record<string, RouteHandler> = {},
): Record<string, RouteHandler> {
  return {
    'GET /api/me/credits': () => jsonResponse(200, world.credits),
    'GET /api/auth/me': ok(USER_A),
    ...extra,
  };
}

interface Mounted {
  readonly fetch: AccountFetch;
  readonly queryClient: QueryClient;
}

function mount(ui: React.JSX.Element, table: Record<string, RouteHandler>): Mounted {
  const fetch = stubAccountFetch(table);
  signInAs(USER_A);
  const queryClient = createAppQueryClient();
  queryClient.setQueryData(currentUserQueryKey, USER_A);
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <WorkspaceScopeProvider scope={{ kind: 'account', userId: USER_A.id }}>
          {ui}
        </WorkspaceScopeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { fetch, queryClient };
}

/** The credits read has settled with data: the positive control for every "shows nothing". */
async function settled(queryClient: QueryClient): Promise<void> {
  await waitFor(() => {
    expect(queryClient.getQueryData(creditsQueryKey(USER_A.id))).toBeDefined();
  });
}

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('CreditsLine', () => {
  it('says the balance is being checked while the read is in flight', async () => {
    mount(<CreditsLine userId={USER_A.id} />, { 'GET /api/me/credits': hang });
    expect(await screen.findByText(CREDITS_LOADING)).toBeVisible();
  });

  it('says the credits could not be loaded when the read fails', async () => {
    mount(<CreditsLine userId={USER_A.id} />, {
      'GET /api/me/credits': status(404, 'not_found'),
    });
    expect(await screen.findByText(CREDITS_LOAD_ERROR)).toBeVisible();
    expect(screen.queryByText(CREDITS_LOADING)).not.toBeInTheDocument();
  });

  it.each([3, 1])('shows the cost and the balance of %i from the server', async (balance) => {
    mount(<CreditsLine userId={USER_A.id} />, routes({ credits: credits({ balance }) }));
    expect(await screen.findByText(creditsLine(balance))).toBeVisible();
    expect(screen.queryByText(CREDITS_LOADING)).not.toBeInTheDocument();
  });
});

describe('CreditsNotice', () => {
  it.each(['credits_introduced', 'first_credit'] as const)('shows the %s copy', async (notice) => {
    mount(<CreditsNotice userId={USER_A.id} />, routes({ credits: credits({ notice }) }));
    expect(await screen.findByText(CREDITS_NOTICE[notice])).toBeVisible();
  });

  it('says that everything made before stays free, in the existing-account variant', async () => {
    mount(
      <CreditsNotice userId={USER_A.id} />,
      routes({ credits: credits({ notice: 'credits_introduced' }) }),
    );
    expect(await screen.findByText(/everything you made before stays free/i)).toBeVisible();
  });

  it('shows nothing when there is no notice', async () => {
    const { queryClient } = mount(
      <CreditsNotice userId={USER_A.id} />,
      routes({ credits: credits() }),
    );
    await settled(queryClient);
    expect(screen.queryByText(CREDITS_NOTICE.credits_introduced)).not.toBeInTheDocument();
    expect(screen.queryByText(CREDITS_NOTICE.first_credit)).not.toBeInTheDocument();
  });

  it('shows nothing while loading and nothing when the read failed', async () => {
    mount(<CreditsNotice userId={USER_A.id} />, {
      'GET /api/me/credits': status(404, 'not_found'),
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByText(CREDITS_LOAD_ERROR)).not.toBeInTheDocument();
    expect(screen.queryByText(CREDITS_NOTICE.credits_introduced)).not.toBeInTheDocument();
  });

  it('disappears once the read says the account has been charged', async () => {
    const { queryClient } = mount(
      <CreditsNotice userId={USER_A.id} />,
      routes({ credits: credits({ notice: 'first_credit' }) }),
    );
    expect(await screen.findByText(CREDITS_NOTICE.first_credit)).toBeVisible();
    queryClient.setQueryData<Credits>(creditsQueryKey(USER_A.id), credits({ notice: null }));
    await waitFor(() => {
      expect(screen.queryByText(CREDITS_NOTICE.first_credit)).not.toBeInTheDocument();
    });
  });
});

describe('OutOfCredits', () => {
  it('says the account is out, that its work stays, and offers both packs from the server', async () => {
    mount(
      <OutOfCredits userId={USER_A.id} />,
      routes({ credits: credits({ balance: 0, out_of_credits: true }) }),
    );
    expect(await screen.findByRole('button', { name: packLabel(3, 500) })).toBeEnabled();
    expect(screen.getByRole('button', { name: packLabel(10, 1200) })).toBeEnabled();
    expect(screen.getByText(OUT_OF_CREDITS_HEADING)).toBeVisible();
    expect(screen.getByText(OUT_OF_CREDITS_KEEP)).toBeVisible();
  });

  it('still says it, with no pack buttons, when the credits read failed (a 409 got here first)', async () => {
    mount(<OutOfCredits userId={USER_A.id} />, {
      'GET /api/me/credits': status(404, 'not_found'),
    });
    expect(await screen.findByText(OUT_OF_CREDITS_HEADING)).toBeVisible();
    expect(screen.getByText(OUT_OF_CREDITS_KEEP)).toBeVisible();
    expect(screen.queryByRole('button', { name: packLabel(3, 500) })).not.toBeInTheDocument();
  });

  it('prices the packs from the server, not from the client', async () => {
    const odd: PackTerms = { id: 'pack_3', credits: 4, price_cents: 750 };
    mount(
      <OutOfCredits userId={USER_A.id} />,
      routes({ credits: credits({ balance: 0, out_of_credits: true, packs: [odd] }) }),
    );
    expect(await screen.findByRole('button', { name: packLabel(4, 750) })).toBeVisible();
    expect(packLabel(4, 750)).toBe('4 credits for $7.50');
  });
});

describe('PackOffer and PackDialog', () => {
  const packClick = (answer: RouteHandler): Record<string, RouteHandler> => ({
    'POST /api/me/pack-clicks': answer,
  });

  async function clickPack(label: string): Promise<void> {
    const button = await screen.findByRole('button', { name: label });
    await waitFor(() => {
      expect(button).toBeEnabled();
    });
    await userEvent.setup().click(button);
  }

  it('shows one button per pack, in the server order', () => {
    mount(
      <PackOffer userId={USER_A.id} packs={[PACK_3, PACK_10]} />,
      routes({ credits: credits() }),
    );
    const buttons = screen.getAllByRole('button');
    expect(buttons.map((b) => b.textContent)).toEqual([packLabel(3, 500), packLabel(10, 1200)]);
  });

  it('shows pending on the clicked button only', async () => {
    mount(
      <PackOffer userId={USER_A.id} packs={[PACK_3, PACK_10]} />,
      routes({ credits: credits() }, packClick(hang)),
    );
    await clickPack(packLabel(3, 500));
    expect(await screen.findByText(PACK_PENDING)).toBeVisible();
    expect(screen.getAllByText(PACK_PENDING)).toHaveLength(1);
    expect(screen.getByRole('button', { name: packLabel(10, 1200) })).toBeEnabled();
  });

  it('sends one request for a same-tick double click on one pack', async () => {
    const { fetch } = mount(
      <PackOffer userId={USER_A.id} packs={[PACK_3, PACK_10]} />,
      routes({ credits: credits() }, packClick(hang)),
    );
    const button = await screen.findByRole('button', { name: packLabel(3, 500) });
    fireEvent.click(button);
    fireEvent.click(button);
    await screen.findByText(PACK_PENDING);
    expect(callsTo(fetch, 'POST', '/api/me/pack-clicks')).toHaveLength(1);
    expect(callsTo(fetch, 'POST', '/api/me/pack-clicks')[0]?.body).toEqual({ pack: 'pack_3' });
  });

  it('opens the dialog saying payments are opening soon and nothing was charged', async () => {
    mount(
      <PackOffer userId={USER_A.id} packs={[PACK_3, PACK_10]} />,
      routes(
        { credits: credits() },
        packClick(() => jsonResponse(200, { payment_notice_opted_in: false })),
      ),
    );
    await clickPack(packLabel(10, 1200));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(PACK_DIALOG_HEADING)).toBeVisible();
    expect(within(dialog).getByText(PACK_OPENING_SOON)).toBeVisible();
    expect(within(dialog).queryByText(PACK_CLICK_ERROR)).not.toBeInTheDocument();
  });

  it('says it did not go through and that nothing was charged when the click fails', async () => {
    mount(
      <PackOffer userId={USER_A.id} packs={[PACK_3, PACK_10]} />,
      routes({ credits: credits() }, packClick(status(503, 'service_unavailable'))),
    );
    await clickPack(packLabel(3, 500));
    expect(await screen.findByText(PACK_CLICK_ERROR)).toBeVisible();
    expect(screen.queryByText(PACK_OPENING_SOON)).not.toBeInTheDocument();
  });

  async function openedDialog(
    world: World,
    optIn: RouteHandler,
    answer: boolean = world.credits.payment_notice_opted_in,
  ): Promise<{ checkbox: HTMLElement; fetch: AccountFetch; queryClient: QueryClient }> {
    const { fetch, queryClient } = mount(
      <PackOffer userId={USER_A.id} packs={[PACK_3, PACK_10]} />,
      routes(world, {
        ...packClick(() => jsonResponse(200, { payment_notice_opted_in: answer })),
        'PUT /api/me/payment-notice': optIn,
      }),
    );
    await clickPack(packLabel(3, 500));
    const checkbox = await screen.findByRole('checkbox', { name: optInLabel(USER_A.email) });
    return { checkbox, fetch, queryClient };
  }

  it('names the account address in the opt-in, unchecked until the account opts in', async () => {
    const { checkbox } = await openedDialog({ credits: credits() }, hang);
    expect(checkbox).not.toBeChecked();
    expect(optInLabel(USER_A.email)).toMatch(/one plain-text email, nothing else/i);
  });

  it('shows the opt-in checked when the account already opted in', async () => {
    const world = { credits: credits({ payment_notice_opted_in: true }) };
    const { checkbox } = await openedDialog(world, hang);
    expect(checkbox).toBeChecked();
  });

  it('shows the opt-in saving while its request is in flight', async () => {
    const { checkbox, fetch } = await openedDialog({ credits: credits() }, hang);
    await userEvent.setup().click(checkbox);
    expect(await screen.findByText(OPT_IN_SAVING)).toBeVisible();
    expect(callsTo(fetch, 'PUT', '/api/me/payment-notice')[0]?.body).toEqual({ opted_in: true });
  });

  it('checks the opt-in from the answer and says nothing wrong when the save succeeds', async () => {
    const world = { credits: credits() };
    const { checkbox } = await openedDialog(world, () => {
      world.credits = { ...world.credits, payment_notice_opted_in: true };
      return jsonResponse(200, { payment_notice_opted_in: true });
    });
    await userEvent.setup().click(checkbox);
    await waitFor(() => {
      expect(screen.getByRole('checkbox', { name: optInLabel(USER_A.email) })).toBeChecked();
    });
    expect(screen.queryByText(OPT_IN_ERROR)).not.toBeInTheDocument();
  });

  it('says the opt-in could not be saved and leaves it unchecked when the save fails', async () => {
    const { checkbox } = await openedDialog(
      { credits: credits() },
      status(503, 'service_unavailable'),
    );
    await userEvent.setup().click(checkbox);
    expect(await screen.findByText(OPT_IN_ERROR)).toBeVisible();
    expect(screen.getByRole('checkbox', { name: optInLabel(USER_A.email) })).not.toBeChecked();
  });

  it('asks its opener to close from the Close button', async () => {
    const onClose = vi.fn();
    mount(
      <PackDialog userId={USER_A.id} pack={PACK_3} onClose={onClose} />,
      routes({ credits: credits() }),
    );
    await userEvent.setup().click(await screen.findByRole('button', { name: CLOSE }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('CreditsSection', () => {
  it('shows its heading and a loading state with no balance while the read is in flight', async () => {
    mount(<CreditsSection userId={USER_A.id} />, { 'GET /api/me/credits': hang });
    expect(await screen.findByRole('heading', { name: CREDITS_HEADING })).toBeVisible();
    expect(screen.queryByText(/^You have \d+ credit/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: RETRY })).not.toBeInTheDocument();
  });

  it('shows an error with Retry, and Retry reads again', async () => {
    let attempt = 0;
    const { fetch } = mount(<CreditsSection userId={USER_A.id} />, {
      'GET /api/me/credits': () => {
        attempt += 1;
        return attempt === 1
          ? jsonResponse(404, { error: { code: 'not_found', message: 'x' } })
          : jsonResponse(200, credits({ balance: 2 }));
      },
    });
    const retry = await screen.findByRole('button', { name: RETRY });
    await userEvent.setup().click(retry);
    expect(await screen.findByText(balanceLine(2))).toBeVisible();
    expect(callsTo(fetch, 'GET', '/api/me/credits')).toHaveLength(2);
  });

  it.each([
    [1, 'You have 1 credit'],
    [3, 'You have 3 credits'],
  ])('says the balance of %i as "%s"', async (balance, line) => {
    mount(<CreditsSection userId={USER_A.id} />, routes({ credits: credits({ balance }) }));
    expect(await screen.findByText(line)).toBeVisible();
    expect(balanceLine(balance)).toBe(line);
  });

  it('says there is no activity yet for an empty ledger', async () => {
    mount(<CreditsSection userId={USER_A.id} />, routes({ credits: credits({ entries: [] }) }));
    expect(await screen.findByText(NO_ENTRIES)).toBeVisible();
  });

  it('lists each ledger line with its label and signed amount', async () => {
    const entries: Credits['entries'] = [
      { kind: 'refund', credits: 1, tailoring_run_id: 'r-1', at: '2026-11-08T10:00:00Z' },
      { kind: 'charge', credits: -1, tailoring_run_id: 'r-1', at: '2026-11-08T09:59:00Z' },
      { kind: 'welcome', credits: 1, tailoring_run_id: null, at: '2026-11-08T09:00:00Z' },
    ];
    mount(<CreditsSection userId={USER_A.id} />, routes({ credits: credits({ entries }) }));
    const items = await screen.findAllByRole('listitem');
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent(ENTRY_LABELS.refund);
    expect(items[0]).toHaveTextContent(signedCredits(1));
    expect(items[1]).toHaveTextContent(ENTRY_LABELS.charge);
    expect(items[1]).toHaveTextContent(signedCredits(-1));
    expect(items[2]).toHaveTextContent(ENTRY_LABELS.welcome);
    expect(screen.queryByText(NO_ENTRIES)).not.toBeInTheDocument();
  });

  it('offers the packs from the read', async () => {
    mount(<CreditsSection userId={USER_A.id} />, routes({ credits: credits() }));
    expect(await screen.findByRole('button', { name: packLabel(3, 500) })).toBeVisible();
    expect(screen.getByRole('button', { name: packLabel(10, 1200) })).toBeVisible();
  });
});

describe('GuestTailorOffer', () => {
  function offer(placement: 'workspace' | 'run'): void {
    render(
      <MemoryRouter>
        <GuestTailorOffer placement={placement} />
      </MemoryRouter>,
    );
  }

  it('offers registration with the free credit under the fit score', () => {
    offer('workspace');
    expect(screen.getByText(GUEST_OFFER_HEADING)).toBeVisible();
    expect(screen.getByText(GUEST_OFFER_WORKSPACE)).toBeVisible();
    expect(screen.queryByText(GUEST_OFFER_RUN)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: GUEST_OFFER_SIGN_UP })).toHaveAttribute(
      'href',
      '/register',
    );
  });

  it('says downloads need an account where the export bar was', () => {
    offer('run');
    expect(screen.getByText(GUEST_OFFER_RUN)).toBeVisible();
    expect(screen.queryByText(GUEST_OFFER_WORKSPACE)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: GUEST_OFFER_SIGN_UP })).toHaveAttribute(
      'href',
      '/register',
    );
  });

  it('links to sign in', () => {
    offer('run');
    expect(screen.getByRole('link', { name: GUEST_OFFER_SIGN_IN })).toHaveAttribute(
      'href',
      '/login',
    );
  });

  it('offers no Tailor button', () => {
    offer('workspace');
    expect(screen.queryByRole('button', { name: /tailor/i })).not.toBeInTheDocument();
  });
});
