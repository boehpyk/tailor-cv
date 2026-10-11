import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import { USER_A, callsTo, signInAs, signedInRoutes, stubAccountFetch } from '@/test/accountFetch';
import {
  GUEST_HOME,
  REPORT,
  adminOnly,
  detail,
  noContent,
  page,
  userGone,
} from '@/test/adminFetch';
import { renderWithRouter } from '@/test/render';

import { DISABLE_LABEL, ENABLE_LABEL, ERASE_LABEL, USERS_CAPTION } from '../adminCopy';

import type { AdminUserDetail } from '@/api/admin';
import type { User } from '@/features/auth/types';

/**
 * T22 (`qa`, test-after) — slice 4.2, AC-38 (query keys, sign-out, invalidation) and AC-39
 * (routing, accessibility). T20 already covers erase's list refetch and detail removal, so those
 * are not repeated. Each claim below was proven by a mutation (recorded in the commit body).
 */

const ADMIN: User = { ...USER_A, id: 'admin-1', email: 'root@example.com', role: 'admin' };
const ANN = detail({ id: 'u-ann', email: 'ann@example.com' });
const ROOT = ['auth', 'account', ADMIN.id, 'admin'];

beforeEach(() => {
  __resetForTests();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function serve(start: AdminUserDetail) {
  let current: AdminUserDetail | null = start;
  const flip = (disabledAt: string | null): Response => {
    if (current === null) return userGone();
    current = { ...current, disabled_at: disabledAt };
    return Response.json({
      user_id: current.id,
      disabled_at: disabledAt,
      changed: true,
      logins_revoked: 0,
    });
  };
  const fetch = stubAccountFetch({
    ...signedInRoutes(ADMIN),
    ...GUEST_HOME,
    'GET /api/admin/access': noContent,
    'GET /api/admin/users': adminOnly(ADMIN, (c, n) => page(current ? [current] : [])(c, n)),
    'GET /api/admin/users/:id': adminOnly(ADMIN, () =>
      current ? Response.json(current) : userGone(),
    ),
    'POST /api/admin/users/:id/disable': adminOnly(ADMIN, () => flip('2026-10-11T08:00:00Z')),
    'POST /api/admin/users/:id/enable': adminOnly(ADMIN, () => flip(null)),
    'DELETE /api/admin/users/:id': adminOnly(ADMIN, () => {
      current = null;
      return Response.json(REPORT);
    }),
  });
  signInAs(ADMIN);
  return fetch;
}

const keysOf = (client: ReturnType<typeof createAppQueryClient>) =>
  client
    .getQueryCache()
    .getAll()
    .map((q) => q.queryKey);

async function click(name: string, scope?: HTMLElement) {
  const root = scope ?? document.body;
  const button = await within(root).findByRole('button', { name });
  await waitFor(() => {
    expect(button).toBeEnabled();
  });
  await userEvent.setup().click(button);
}

describe('AC-38 — keys', () => {
  it("the directory and the detail live under the actor's account root", async () => {
    serve(ANN);
    const queryClient = createAppQueryClient();
    renderWithRouter('/admin?q=ann', { queryClient });
    await userEvent.setup().click(await screen.findByRole('link', { name: 'ann@example.com' }));
    await screen.findByRole('heading', { level: 1, name: 'ann@example.com' });

    const keys = keysOf(queryClient);
    expect(keys).toContainEqual([...ROOT, 'users', 'ann']);
    expect(keys).toContainEqual([...ROOT, 'user', 'u-ann']);
  });

  it('disabling your own account drops every admin key', async () => {
    serve({ ...ANN, id: ADMIN.id, email: ADMIN.email });
    const queryClient = createAppQueryClient();
    renderWithRouter(`/admin/users/${ADMIN.id}`, { queryClient });
    await click(DISABLE_LABEL);
    await click(DISABLE_LABEL, await screen.findByRole('alertdialog'));

    await waitFor(() => {
      expect(keysOf(queryClient).filter((k) => k.includes('admin'))).toEqual([]);
    });
  });
});

describe('AC-38 — invalidation after an action', () => {
  async function actThenGoBack(start: AdminUserDetail, label: string, confirm: boolean) {
    const fetch = serve(start);
    const { router } = renderWithRouter('/admin', { queryClient: createAppQueryClient() });
    await userEvent.setup().click(await screen.findByRole('link', { name: start.email }));
    await click(label);
    if (confirm) await click(label, await screen.findByRole('alertdialog'));
    return { fetch, router };
  }

  it('disable: the directory shows the new status when you go back', async () => {
    const { fetch, router } = await actThenGoBack(ANN, DISABLE_LABEL, true);
    await screen.findByRole('button', { name: ENABLE_LABEL });

    await router.navigate(-1);

    const row = await screen.findByRole('row', { name: /ann@example\.com/ });
    await waitFor(() => {
      expect(row).toHaveTextContent('Disabled');
    });
    expect(callsTo(fetch, 'GET', '/api/admin/users').length).toBeGreaterThanOrEqual(2);
  });

  it('enable: the directory shows the new status when you go back', async () => {
    const { router } = await actThenGoBack(
      { ...ANN, disabled_at: '2026-10-01T09:00:00Z' },
      ENABLE_LABEL,
      false,
    );
    await screen.findByRole('button', { name: DISABLE_LABEL });

    await router.navigate(-1);

    const row = await screen.findByRole('row', { name: /ann@example\.com/ });
    await waitFor(() => {
      expect(row).toHaveTextContent('Active');
    });
  });
});

describe('AC-39 — routing', () => {
  it.each(['/admin/whatever', '/admin/users'])(
    '%s renders the app not-found page',
    async (path) => {
      serve(ANN);
      renderWithRouter(path, { queryClient: createAppQueryClient() });

      expect(
        await screen.findByRole('heading', { name: /couldn.t find that page/i }),
      ).toBeInTheDocument();
    },
  );
});

describe('AC-39 — accessibility', () => {
  it('the list has one h1 and a captioned table; nothing takes focus on load', async () => {
    serve(ANN);
    renderWithRouter('/admin', { queryClient: createAppQueryClient() });

    const table = await screen.findByRole('table', { name: USERS_CAPTION });
    expect(table).toBeInTheDocument();
    // The layout owns one h1 (the site title); the page adds exactly one.
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(2);
    expect(document.body).toHaveFocus();
  });

  it('the account page has one h1 and takes no focus on load', async () => {
    serve(ANN);
    renderWithRouter('/admin/users/u-ann', { queryClient: createAppQueryClient() });

    await screen.findByRole('heading', { level: 1, name: 'ann@example.com' });
    // The layout owns one h1 (the site title); the page adds exactly one.
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(2);
    expect(document.body).toHaveFocus();
  });

  it.each([DISABLE_LABEL, ERASE_LABEL])(
    'the %s dialog takes focus inside, and Cancel returns it to the opener',
    async (label) => {
      serve(ANN);
      renderWithRouter('/admin/users/u-ann', { queryClient: createAppQueryClient() });
      await click(label);
      const dialog = await screen.findByRole('alertdialog');
      expect(dialog).toContainElement(document.activeElement as HTMLElement);

      await userEvent.setup().click(within(dialog).getByRole('button', { name: /cancel/i }));

      await waitFor(() => {
        expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
      });
      expect(screen.getByRole('button', { name: label })).toHaveFocus();
    },
  );
});
