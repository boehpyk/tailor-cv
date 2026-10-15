import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import { callsTo, hang, signInAs, status, stubAccountFetch } from '@/test/accountFetch';
import {
  ADMIN,
  OTHER_SESSION_ID,
  SESSION_ID,
  guestRoutes,
  guestWorld,
  sessionSummary,
} from '@/test/adminGuestsFetch';
import { renderWithRouter } from '@/test/render';

import { GUESTS_ERROR, LOAD_MORE_LABEL } from '../adminCopy';

import type { GuestWorld } from '@/test/adminGuestsFetch';
import type { RouteHandler } from '@/test/accountFetch';

/**
 * T21 RED — slice 4.4, AC-32 (/admin/guests). Against T20's skeleton (an empty div) each fails on
 * an absent element.
 */

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const SLOW = { timeout: 4000 };
const LIST = '/api/admin/guest-sessions';

function open(w: GuestWorld = guestWorld(), overrides: Record<string, RouteHandler> = {}) {
  const fetch = stubAccountFetch(guestRoutes(w, overrides));
  signInAs(ADMIN);
  const view = renderWithRouter('/admin/guests', { queryClient: createAppQueryClient() });
  return { fetch, ...view };
}

describe('the guest list — AC-32', () => {
  it('loading: a status line while the first page is pending', async () => {
    const { fetch } = open(guestWorld(), { [`GET ${LIST}`]: hang });

    // Not the lazy chunk's Suspense fallback (also a status): wait until the page asked.
    await waitFor(() => {
      expect(callsTo(fetch, 'GET', LIST)).toHaveLength(1);
    });
    expect(within(screen.getByRole('main')).getByText('Loading guest sessions…')).toHaveAttribute(
      'role',
      'status',
    );
  });

  it('empty: "No live guest sessions."', async () => {
    open(guestWorld({ sessionPages: [[]] }));

    expect(await screen.findByText('No live guest sessions.')).toBeVisible();
  });

  it('a 503 is an alert with Retry, distinct from loading; Retry shows the rows', async () => {
    let calls = 0;
    open(guestWorld(), {
      [`GET ${LIST}`]: () => {
        calls += 1;
        return calls <= 2
          ? status(503, 'service_unavailable')()
          : Response.json({ items: [sessionSummary()], next_cursor: null });
      },
    });

    const alert = await screen.findByRole('alert', {}, SLOW);
    expect(within(alert).getByText(GUESTS_ERROR)).toBeVisible();
    await userEvent.setup().click(within(alert).getByRole('button', { name: 'Retry' }));

    await waitFor(() => {
      expect(
        screen.getByRole('main').querySelector(`a[href="/admin/guests/${SESSION_ID}"]`),
      ).not.toBeNull();
    });
  });

  it('a row links to its session page and shows the four counts and the expiry time', async () => {
    open();

    const main = screen.getByRole('main');
    const link = await waitFor(() => {
      const found = main.querySelector(`a[href="/admin/guests/${SESSION_ID}"]`);
      if (found === null) throw new Error('no session link yet');
      return found;
    });
    const row = link.closest('tr, li');
    expect(row).not.toBeNull();
    for (const count of ['3', '4', '5', '6']) {
      expect(row).toHaveTextContent(new RegExp(`\\b${count}\\b`));
    }
    expect(row).toHaveTextContent(/\d{1,2}:\d{2}/);
    expect(screen.getAllByText(/expires at/i).length).toBeGreaterThan(0);
  });

  it('Load more sends the cursor, appends, and vanishes on the last page', async () => {
    const w = guestWorld({
      sessionPages: [[sessionSummary()], [sessionSummary({ id: OTHER_SESSION_ID })]],
    });
    const { fetch } = open(w);

    const more = await screen.findByRole('button', { name: LOAD_MORE_LABEL });
    await waitFor(() => {
      expect(more).toBeEnabled();
    });
    await userEvent.setup().click(more);

    await waitFor(() => {
      expect(
        screen.getByRole('main').querySelector(`a[href="/admin/guests/${OTHER_SESSION_ID}"]`),
      ).not.toBeNull();
    });
    expect(
      screen.getByRole('main').querySelector(`a[href="/admin/guests/${SESSION_ID}"]`),
    ).not.toBeNull();
    expect(callsTo(fetch, 'GET', LIST).map((call) => call.query.get('cursor'))).toEqual([
      null,
      'c1',
    ]);
    expect(screen.queryByRole('button', { name: LOAD_MORE_LABEL })).not.toBeInTheDocument();
  });

  it('no Load more on a single page', async () => {
    open();

    await waitFor(() => {
      expect(
        screen.getByRole('main').querySelector(`a[href="/admin/guests/${SESSION_ID}"]`),
      ).not.toBeNull();
    });
    expect(screen.queryByRole('button', { name: LOAD_MORE_LABEL })).not.toBeInTheDocument();
  });

  it('Load more is disabled while the next page is fetching', async () => {
    const w = guestWorld({
      sessionPages: [[sessionSummary()], [sessionSummary({ id: OTHER_SESSION_ID })]],
    });
    open(w, {
      [`GET ${LIST}`]: (call) =>
        call.query.get('cursor') === 'c1'
          ? hang()
          : Response.json({ items: [sessionSummary()], next_cursor: 'c1' }),
    });

    const more = await screen.findByRole('button', { name: LOAD_MORE_LABEL });
    await waitFor(() => {
      expect(more).toBeEnabled();
    });
    fireEvent.click(more);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /loading more|load more/i })).toBeDisabled();
    });
  });

  it('exactly one h1 inside main', async () => {
    open(guestWorld({ sessionPages: [[]] }));

    await screen.findByText('No live guest sessions.');
    expect(within(screen.getByRole('main')).getAllByRole('heading', { level: 1 })).toHaveLength(1);
  });
});
