import { QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import {
  USER_A,
  callsTo,
  hang,
  signInAs,
  signedInRoutes,
  status,
  stubAccountFetch,
} from '@/test/accountFetch';
import { adminOnly, noContent, page, summary } from '@/test/adminFetch';
import { routes } from '@/router';
import { renderWithRouter } from '@/test/render';

import {
  ADMIN_RETRY_LABEL,
  LOAD_MORE_LABEL,
  SEARCH_INVALID,
  SEARCH_LABEL,
  SEARCH_SUBMIT_LABEL,
  USERS_ERROR,
  USERS_HEADING,
  USERS_LOADING,
  YOU_MARKER,
  usersNoMatch,
} from '../adminCopy';
import { erasedNoticeState } from '../erasedNotice';

import type { InitialEntry } from 'react-router';
import type { AdminUserSummary } from '@/api/admin';
import type { User } from '@/features/auth/types';

/**
 * T20 RED — slice 4.2, AC-31 (the user list's states) and AC-32 (search), mounted through the real
 * route table. Written from the spec; against T19's skeleton (`UserListPage` fetches nothing and
 * renders an empty table) each fails on an absent element, never on an import.
 */

const ADMIN: User = { ...USER_A, id: 'admin-1', email: 'root@example.com', role: 'admin' };
const ANN = summary({ id: 'u-ann', email: 'ann@example.com' });
const BOB = summary({
  id: 'u-bob',
  email: 'bob@example.com',
  role: 'admin',
  disabled_at: '2026-10-01T09:00:00Z',
});
const ME = summary({ id: ADMIN.id, email: ADMIN.email, role: 'admin' });

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function serve(list: Parameters<typeof adminOnly>[1]): ReturnType<typeof stubAccountFetch> {
  const fetch = stubAccountFetch({
    ...signedInRoutes(ADMIN),
    'GET /api/admin/access': noContent,
    'GET /api/admin/users': adminOnly(ADMIN, list),
  });
  signInAs(ADMIN);
  return fetch;
}

const open = (path = '/admin') => renderWithRouter(path, { queryClient: createAppQueryClient() });
const listCalls = (fetch: ReturnType<typeof stubAccountFetch>) =>
  callsTo(fetch, 'GET', '/api/admin/users');

describe('/admin user list — AC-31', () => {
  it('loading: a status "Loading users…" and no table rows', async () => {
    serve(hang);
    open();

    const loading = await screen.findByText(USERS_LOADING);
    expect(loading).toHaveAttribute('role', 'status');
  });

  it('rows: email, role, status and created date, and only the actor marked You', async () => {
    serve(page([ME, ANN, BOB]));
    open();

    const table = await screen.findByRole('table');
    const rows = within(table).getAllByRole('row');
    // header + three accounts
    expect(rows).toHaveLength(4);
    const ann = within(table).getByRole('row', { name: /ann@example\.com/ });
    expect(ann).toHaveTextContent('Active');
    const bob = within(table).getByRole('row', { name: /bob@example\.com/ });
    expect(bob).toHaveTextContent('Disabled');
    expect(bob).toHaveTextContent('Admin');
    expect(within(table).getAllByText(YOU_MARKER)).toHaveLength(1);
    expect(within(table).getByRole('row', { name: /root@example\.com/ })).toHaveTextContent(
      YOU_MARKER,
    );
    expect(ann).not.toHaveTextContent(YOU_MARKER);
  });

  it('each email links to its account page', async () => {
    serve(page([ANN]));
    open();

    const link = await screen.findByRole('link', { name: 'ann@example.com' });
    expect(link).toHaveAttribute('href', '/admin/users/u-ann');
  });

  it('empty with a search: "No users match “zed”."', async () => {
    serve(page([]));
    open('/admin?q=zed');

    expect(await screen.findByText(usersNoMatch('zed'))).toBeInTheDocument();
  });

  it('error: an alert with Retry, no loading status; Retry refetches and shows the rows', async () => {
    const fetch = serve((call, n) =>
      n <= 2 ? status(503, 'service_unavailable')() : page([ANN])(call, n),
    );
    open();

    const alert = await screen.findByRole('alert', {}, { timeout: 4000 });
    expect(alert).toHaveTextContent(USERS_ERROR);
    expect(screen.queryByText(USERS_LOADING)).not.toBeInTheDocument();
    const before = listCalls(fetch).length;
    await userEvent.setup().click(screen.getByRole('button', { name: ADMIN_RETRY_LABEL }));

    expect(await screen.findByRole('link', { name: 'ann@example.com' })).toBeInTheDocument();
    expect(listCalls(fetch).length).toBeGreaterThan(before);
  });

  it('Load more while next_cursor is set: sends the cursor, appends, and vanishes on the last page', async () => {
    const fetch = serve((call, n) =>
      call.query.get('cursor') === 'c1' ? page([BOB])(call, n) : page([ANN], 'c1')(call, n),
    );
    open();

    await screen.findByRole('link', { name: 'ann@example.com' });
    const more = screen.getByRole('button', { name: LOAD_MORE_LABEL });
    await waitFor(() => {
      expect(more).toBeEnabled();
    });
    await userEvent.setup().click(more);

    expect(await screen.findByRole('link', { name: 'bob@example.com' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'ann@example.com' })).toBeInTheDocument();
    expect(listCalls(fetch).map((call) => call.query.get('cursor'))).toEqual([null, 'c1']);
    expect(screen.queryByRole('button', { name: LOAD_MORE_LABEL })).not.toBeInTheDocument();
  });

  it('no Load more on a single page', async () => {
    serve(page([ANN]));
    open();

    await screen.findByRole('link', { name: 'ann@example.com' });
    expect(screen.queryByRole('button', { name: LOAD_MORE_LABEL })).not.toBeInTheDocument();
  });

  it('Load more is disabled while the next page is fetching', async () => {
    serve((call, n) => (call.query.get('cursor') === 'c1' ? hang() : page([ANN], 'c1')(call, n)));
    open();

    await screen.findByRole('link', { name: 'ann@example.com' });
    const more = screen.getByRole('button', { name: LOAD_MORE_LABEL });
    await waitFor(() => {
      expect(more).toBeEnabled();
    });
    fireEvent.click(more);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /loading more|load more/i })).toBeDisabled();
    });
  });
});

describe('/admin user list — AC-32 search', () => {
  it('a form role=search with a labelled input and a Search button', async () => {
    serve(page([ANN]));
    open();

    const form = await screen.findByRole('search');
    expect(within(form).getByLabelText(SEARCH_LABEL)).toBeInTheDocument();
    expect(within(form).getByRole('button', { name: SEARCH_SUBMIT_LABEL })).toBeInTheDocument();
  });

  it('submitting writes ?q= to the URL and refetches from page 1 with that q', async () => {
    const fetch = serve((call, n) =>
      call.query.get('q') === 'ann' ? page([ANN])(call, n) : page([ANN, BOB], 'c1')(call, n),
    );
    const { router } = open();
    await screen.findByRole('link', { name: 'bob@example.com' });

    const user = userEvent.setup();
    await user.type(screen.getByLabelText(SEARCH_LABEL), 'ann');
    await user.click(screen.getByRole('button', { name: SEARCH_SUBMIT_LABEL }));

    await waitFor(() => {
      expect(router.state.location.search).toBe('?q=ann');
    });
    await waitFor(() => {
      expect(screen.queryByRole('link', { name: 'bob@example.com' })).not.toBeInTheDocument();
    });
    const last = listCalls(fetch).at(-1);
    expect(last?.query.get('q')).toBe('ann');
    expect(last?.query.get('cursor')).toBeNull();
  });

  it('typing sends no request until the form is submitted', async () => {
    const fetch = serve(page([ANN]));
    open();
    await screen.findByRole('link', { name: 'ann@example.com' });
    const before = listCalls(fetch).length;

    await userEvent.setup().type(screen.getByLabelText(SEARCH_LABEL), 'ann');

    expect(listCalls(fetch)).toHaveLength(before);
  });

  it('clearing the box and submitting shows everyone again', async () => {
    const fetch = serve((call, n) =>
      call.query.get('q') === 'ann' ? page([ANN])(call, n) : page([ANN, BOB])(call, n),
    );
    const { router } = open('/admin?q=ann');
    await screen.findByRole('link', { name: 'ann@example.com' });
    expect(screen.queryByRole('link', { name: 'bob@example.com' })).not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.clear(screen.getByLabelText(SEARCH_LABEL));
    await user.click(screen.getByRole('button', { name: SEARCH_SUBMIT_LABEL }));

    expect(await screen.findByRole('link', { name: 'bob@example.com' })).toBeInTheDocument();
    expect(router.state.location.search).toBe('');
    expect(listCalls(fetch).at(-1)?.query.has('q')).toBe(false);
  });

  it('back and forward restore the typed search in the box', async () => {
    serve(page([ANN]));
    const { router } = open();
    await screen.findByRole('link', { name: 'ann@example.com' });

    const user = userEvent.setup();
    await user.type(screen.getByLabelText(SEARCH_LABEL), 'ann');
    await user.click(screen.getByRole('button', { name: SEARCH_SUBMIT_LABEL }));
    await waitFor(() => {
      expect(router.state.location.search).toBe('?q=ann');
    });
    expect(screen.getByLabelText(SEARCH_LABEL)).toHaveValue('ann');

    await router.navigate(-1);
    await waitFor(() => {
      expect(screen.getByLabelText(SEARCH_LABEL)).toHaveValue('');
    });
    await router.navigate(1);
    await waitFor(() => {
      expect(screen.getByLabelText(SEARCH_LABEL)).toHaveValue('ann');
    });
  });

  it('a 422 invalid_search says "That search can\'t be used." and keeps the typed text', async () => {
    serve((call, n) =>
      call.query.get('q') === '%%'
        ? status(422, 'invalid_search')()
        : page([ANN] satisfies AdminUserSummary[])(call, n),
    );
    open();
    await screen.findByRole('link', { name: 'ann@example.com' });

    const user = userEvent.setup();
    await user.type(screen.getByLabelText(SEARCH_LABEL), '%%');
    await user.click(screen.getByRole('button', { name: SEARCH_SUBMIT_LABEL }));

    expect(await screen.findByText(SEARCH_INVALID)).toBeInTheDocument();
    expect(screen.getByLabelText(SEARCH_LABEL)).toHaveValue('%%');
    expect(screen.queryByRole('heading', { name: USERS_HEADING })).toBeInTheDocument();
  });
});

describe('UserListPage: the erase notice leaves no email in history (AC-35, AC-38)', () => {
  /**
   * Mutation (recorded 2026-10-11): replacing the `navigate(…)` call in `UserListPage`'s effect
   * with a no-op turns this red: `AssertionError: expected { erased: { …(2) } } to be null` on
   * `router.state.location.state`; restored byte-exact. (Dropping only `state: null` stays green:
   * a `navigate` without `state` also lands on null, so the option is belt and braces.)
   */
  it('shows the notice once, then replaces the entry with the same URL and no state', async () => {
    const marker = 'erased-marker-4471@example.com';
    serve(page([ANN] satisfies AdminUserSummary[]));
    const queryClient = createAppQueryClient();
    const mount = (entry: InitialEntry[]) => {
      const router = createMemoryRouter(routes, { initialEntries: entry });
      const view = render(
        <QueryClientProvider client={queryClient}>
          <RouterProvider router={router} />
        </QueryClientProvider>,
      );
      return { router, view };
    };

    const first = mount([
      {
        pathname: '/admin',
        search: '?q=x',
        state: erasedNoticeState({ email: marker, unlinkFailures: 2 }),
      },
    ]);
    expect(await screen.findByText(`Erased the account ${marker}.`)).toBeInTheDocument();
    expect(
      screen.getByText('2 file(s) could not be removed; the orphan sweep will reclaim them.'),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(first.router.state.location.state).toBeNull();
    });
    expect(first.router.state.location.pathname).toBe('/admin');
    expect(first.router.state.location.search).toBe('?q=x');

    // Arrive again from where the history now stands (a reload, or Back to this entry).
    first.view.unmount();
    const { pathname, search, state } = first.router.state.location;
    mount([{ pathname, search, state }]);
    await screen.findByRole('link', { name: 'ann@example.com' });
    expect(screen.queryByText(/Erased the account/)).not.toBeInTheDocument();
    expect(JSON.stringify(state)).not.toContain(marker);
  });
});
