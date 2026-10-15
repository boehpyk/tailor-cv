import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import { callsTo, hang, signInAs, stubAccountFetch } from '@/test/accountFetch';
import { ADMIN, SESSION_ID, guestRoutes, guestWorld } from '@/test/adminGuestsFetch';
import { renderWithRouter } from '@/test/render';

import { CANCEL_LABEL, DELETE_GUEST_LABEL, STORAGE_LOADING } from '../adminCopy';

import type { RouteHandler } from '@/test/accountFetch';
import type { QueryClient } from '@tanstack/react-query';

/**
 * T23 (`qa`, test-after) — slice 4.4, AC-40: query keys, what drops them, what refetches them,
 * routing and accessibility of the guest and storage pages. The key claims were proven by
 * mutations (14, recorded in the commit body). The 360 px no-horizontal-scroll claim needs a
 * layout engine; jsdom has none, so it is the manual pass's.
 */

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const SLOW = { timeout: 4000 };
const BASE = `/admin/guests/${SESSION_ID}`;
const ROOT = ['auth', 'account', ADMIN.id, 'admin'];
const SESSION_KEY = [...ROOT, 'guest', SESSION_ID];

function open(path: string, overrides: Record<string, RouteHandler> = {}) {
  const fetch = stubAccountFetch({
    ...guestRoutes(guestWorld(), overrides),
    'POST /api/auth/logout': () => new Response(null, { status: 204 }),
  });
  signInAs(ADMIN);
  const queryClient = createAppQueryClient();
  const view = renderWithRouter(path, { queryClient });
  return { fetch, queryClient, ...view };
}

const keysOf = (client: QueryClient): readonly (readonly unknown[])[] =>
  client
    .getQueryCache()
    .getAll()
    .map((q) => q.queryKey);

const underSession = (client: QueryClient) =>
  keysOf(client).filter((key) => SESSION_KEY.every((part, i) => key[i] === part));

async function click(name: string, scope?: HTMLElement) {
  const button = await within(scope ?? document.body).findByRole('button', { name });
  await waitFor(() => {
    expect(button).toBeEnabled();
  });
  await userEvent.setup().click(button);
}

describe('AC-40 — keys', () => {
  it("a session's overview and its read pages live under adminGuestKey", async () => {
    const { queryClient, router } = open(`${BASE}/cvs/cv-1`);
    await screen.findByText('Guest CV text.');
    await router.navigate(`${BASE}/runs/run-1`);
    await screen.findByRole('heading', { level: 2, name: 'Tailored CV' });

    const keys = keysOf(queryClient);
    expect(keys).toContainEqual([...SESSION_KEY, 'content', 'cv', 'cv-1']);
    expect(keys).toContainEqual([...SESSION_KEY, 'content', 'run', 'run-1']);
    expect(keys).toContainEqual([...SESSION_KEY, 'content', 'run', 'run-1', 'exports']);
  });

  it('the overview is the session key itself, the list a sibling, the storage under the admin root', async () => {
    const { queryClient, router } = open('/admin/guests');
    await screen.findByText(/g-1/);
    await router.navigate(BASE);
    await screen.findByRole('heading', { level: 1, name: 'Guest session' });
    await router.navigate('/admin/storage');
    await screen.findByRole('table');

    const keys = keysOf(queryClient);
    expect(keys).toContainEqual(SESSION_KEY);
    expect(keys).toContainEqual([...ROOT, 'guests']);
    expect(keys).toContainEqual([...ROOT, 'storage']);
  });
});

describe('AC-40 — what drops them', () => {
  it('deleting the session drops everything under its key, content included, and refetches the list', async () => {
    const { queryClient, router, fetch } = open('/admin/guests');
    await screen.findByText(/g-1/);
    await router.navigate(`${BASE}/cvs/cv-1`);
    await screen.findByText('Guest CV text.');
    await router.navigate(BASE);
    await screen.findByRole('heading', { level: 1, name: 'Guest session' });
    expect(underSession(queryClient).length).toBeGreaterThan(1);

    await click(DELETE_GUEST_LABEL);
    await click(DELETE_GUEST_LABEL, await screen.findByRole('alertdialog'));

    expect(await screen.findByText('No live guest sessions.')).toBeVisible();
    expect(underSession(queryClient)).toEqual([]);
    // Within the 30 s staleTime a remount would not refetch: this is the invalidation.
    expect(callsTo(fetch, 'GET', '/api/admin/guest-sessions').length).toBeGreaterThanOrEqual(2);
  });

  it('logging out drops the guest, list and storage keys', async () => {
    const { queryClient, router } = open(BASE);
    await screen.findByRole('heading', { level: 1, name: 'Guest session' });
    await router.navigate('/admin/storage');
    await screen.findByRole('table');
    expect(keysOf(queryClient)).toContainEqual([...ROOT, 'storage']);

    await click('Log out');

    await waitFor(() => {
      expect(keysOf(queryClient).filter((key) => key.includes('admin'))).toEqual([]);
    });
  });
});

describe('AC-40 — routing', () => {
  it.each([`${BASE}/whatever`, `${BASE}/cvs`, `${BASE}/runs/a/b`, '/admin/storage/x'])(
    '%s renders the app not-found page',
    async (path) => {
      open(path);

      expect(
        await screen.findByRole('heading', { name: /couldn.t find that page/i }, SLOW),
      ).toBeInTheDocument();
    },
  );
});

describe('AC-40 — accessibility', () => {
  it.each([
    ['the guest list', '/admin/guests'],
    ['a guest session', BASE],
    ['the storage page', '/admin/storage'],
    ['a guest CV page', `${BASE}/cvs/cv-1`],
  ])('%s has one h1 inside main and takes no focus on load', async (_name, path) => {
    open(path);

    await waitFor(() => {
      expect(within(screen.getByRole('main')).getAllByRole('heading', { level: 1 })).toHaveLength(
        1,
      );
    });
    await waitFor(() => {
      expect(within(screen.getByRole('main')).queryByText(/^Loading/)).not.toBeInTheDocument();
    }, SLOW);
    expect(within(screen.getByRole('main')).getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(document.body).toHaveFocus();
  });

  it('the storage loading line is a status', async () => {
    open('/admin/storage', { 'GET /api/admin/storage': hang });

    expect(await within(screen.getByRole('main')).findByText(STORAGE_LOADING)).toHaveAttribute(
      'role',
      'status',
    );
  });

  it('the Delete guest session dialog takes focus inside, and Cancel returns it to the opener', async () => {
    open(BASE);
    await click(DELETE_GUEST_LABEL);
    const dialog = await screen.findByRole('alertdialog');
    expect(dialog).toContainElement(document.activeElement as HTMLElement);

    await userEvent.setup().click(within(dialog).getByRole('button', { name: CANCEL_LABEL }));

    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: DELETE_GUEST_LABEL })).toHaveFocus();
  });
});
