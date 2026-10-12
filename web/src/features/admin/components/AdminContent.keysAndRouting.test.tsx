import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import { callsTo, signInAs, stubAccountFetch } from '@/test/accountFetch';
import { ADMIN, TARGET_ID, contentRoutes, world } from '@/test/adminContentFetch';
import { REPORT, userGone } from '@/test/adminFetch';
import { renderWithRouter } from '@/test/render';

import { CANCEL_LABEL, DELETE_CV_LABEL, ERASE_LABEL } from '../adminCopy';

import type { RouteHandler } from '@/test/accountFetch';
import type { QueryClient } from '@tanstack/react-query';

/**
 * T14 (`qa`, test-after) — slice 4.3, AC-32: query keys, what drops them, what refetches them,
 * routing and accessibility. Each claim was proven by a mutation (the key one recorded in the
 * commit body). The 360 px no-horizontal-scroll claim needs a layout engine; jsdom has none, so it
 * is the manual pass's (T18).
 */

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const P = '/api/admin/users/:id';
const DETAIL = ['auth', 'account', ADMIN.id, 'admin', 'user', TARGET_ID];
const ACCOUNT = `/admin/users/${TARGET_ID}`;

function serve() {
  // A real server answers 404 for an erased account's content; so does this fake once it is erased.
  let erased = false;
  const unlessErased =
    (handler: RouteHandler): RouteHandler =>
    (call, n) =>
      erased ? userGone() : handler(call, n);
  const defaults = contentRoutes(world());
  const fetch = stubAccountFetch({
    ...defaults,
    'POST /api/auth/logout': () => new Response(null, { status: 204 }),
    [`GET ${P}/base-cvs`]: unlessErased(defaults[`GET ${P}/base-cvs`] as RouteHandler),
    [`GET ${P}/job-postings`]: unlessErased(defaults[`GET ${P}/job-postings`] as RouteHandler),
    [`GET ${P}/tailoring-runs`]: unlessErased(defaults[`GET ${P}/tailoring-runs`] as RouteHandler),
    [`DELETE ${P}`]: () => {
      erased = true;
      return Response.json(REPORT);
    },
  });
  signInAs(ADMIN);
  return fetch;
}

const keysOf = (client: QueryClient): readonly (readonly unknown[])[] =>
  client
    .getQueryCache()
    .getAll()
    .map((q) => q.queryKey);

/**
 * The bodies the admin has loaded: content queries holding data. A mounted page recreates its
 * query when a prefix is removed under it (and the server then answers 404, so it holds an error,
 * never a body) — what must not survive is data.
 */
const loadedBodies = (client: QueryClient) =>
  client
    .getQueryCache()
    .getAll()
    .filter((q) => q.queryKey.includes('content') && q.state.data !== undefined)
    .map((q) => q.queryKey);

async function loadedAccountPage(client: QueryClient) {
  const view = renderWithRouter(ACCOUNT, { queryClient: client });
  await screen.findByRole('link', { name: /Backend roles/ });
  await screen.findAllByRole('link', { name: /Senior Platform Engineer/ });
  return view;
}

async function click(name: string, scope?: HTMLElement) {
  const button = await within(scope ?? document.body).findByRole('button', { name });
  await waitFor(() => {
    expect(button).toBeEnabled();
  });
  await userEvent.setup().click(button);
}

describe('AC-32 — keys', () => {
  it("the three lists live under the account's detail key", async () => {
    serve();
    const client = createAppQueryClient();
    await loadedAccountPage(client);

    const keys = keysOf(client);
    for (const part of ['cvs', 'postings', 'history']) {
      expect(keys).toContainEqual([...DETAIL, 'content', part]);
    }
  });

  it('a CV page loads its CV under the same prefix', async () => {
    serve();
    const client = createAppQueryClient();
    await loadedAccountPage(client);

    await userEvent.setup().click(screen.getByRole('link', { name: /Backend roles/ }));
    await screen.findByRole('heading', { level: 1, name: 'Backend roles' });
    expect(keysOf(client)).toContainEqual([...DETAIL, 'content', 'cv', 'cv-1']);
  });

  it('a run page loads its run and its exports under the detail key', async () => {
    serve();
    const client = createAppQueryClient();
    renderWithRouter(`${ACCOUNT}/runs/run-1`, { queryClient: client });
    await screen.findByRole('heading', { level: 2, name: 'Tailored CV' });

    const keys = keysOf(client);
    expect(keys).toContainEqual([...DETAIL, 'content', 'run', 'run-1']);
    expect(keys).toContainEqual([...DETAIL, 'content', 'run', 'run-1', 'exports']);
  });
});

describe('AC-32 — what drops them', () => {
  /**
   * **Holding pen for a production defect `qa` found** (CLAUDE.md: a strict xfail; `it.fails` fails
   * if the test starts passing, so the fix must remove this marker). AC-32 says 4.2's erase drops
   * every body loaded under the detail key, but `useAccountAction` removes it with
   * `removeQueries({ queryKey: detailKey, exact: true })`, which leaves every `'content'` child —
   * CV text, posting text, both documents — in the cache for its 5-minute `gcTime`. The fix is to
   * drop `exact: true` (react-dev); 4.2's own detail-removal test is the thing to re-check.
   */
  it.fails("4.2's erase of the account drops every body the admin loaded", async () => {
    serve();
    const client = createAppQueryClient();
    await loadedAccountPage(client);
    expect(loadedBodies(client).length).toBeGreaterThan(0);

    await click(ERASE_LABEL);
    await click(ERASE_LABEL, await screen.findByRole('alertdialog'));

    await waitFor(() => {
      expect(loadedBodies(client)).toEqual([]);
    });
  });

  it('logging out drops them all', async () => {
    serve();
    const client = createAppQueryClient();
    await loadedAccountPage(client);
    expect(loadedBodies(client).length).toBeGreaterThan(0);

    await click('Log out');

    await waitFor(() => {
      expect(loadedBodies(client)).toEqual([]);
    });
  });
});

describe('AC-32 — what refetches them', () => {
  it('a delete refetches the lists and the overview counts and drops the item', async () => {
    const fetch = serve();
    const client = createAppQueryClient();
    await loadedAccountPage(client);
    await userEvent.setup().click(screen.getByRole('link', { name: /Backend roles/ }));
    await click(DELETE_CV_LABEL);
    await click(DELETE_CV_LABEL, await screen.findByRole('alertdialog'));

    expect(await screen.findByText('No saved CVs.')).toBeVisible();

    // Within the 30 s staleTime a remount would not refetch: these are invalidations.
    expect(callsTo(fetch, 'GET', P).length).toBeGreaterThanOrEqual(2);
    expect(callsTo(fetch, 'GET', `${P}/base-cvs`).length).toBeGreaterThanOrEqual(2);
    expect(keysOf(client)).not.toContainEqual([...DETAIL, 'content', 'cv', 'cv-1']);
  });
});

describe('AC-32 — routing', () => {
  it.each([`${ACCOUNT}/whatever`, `${ACCOUNT}/cvs`, `${ACCOUNT}/runs/a/b`])(
    '%s renders the app not-found page',
    async (path) => {
      serve();
      renderWithRouter(path, { queryClient: createAppQueryClient() });

      expect(
        await screen.findByRole('heading', { name: /couldn.t find that page/i }),
      ).toBeInTheDocument();
    },
  );
});

describe('AC-32 — accessibility', () => {
  it('the account page keeps one h1 of its own, three h2 sections, and takes no focus on load', async () => {
    serve();
    await loadedAccountPage(createAppQueryClient());

    const main = screen.getByRole('main');
    expect(within(main).getAllByRole('heading', { level: 1 })).toHaveLength(1);
    for (const name of ['Saved CVs', 'History', 'Job postings']) {
      expect(within(main).getByRole('heading', { level: 2, name })).toBeVisible();
    }
    expect(document.body).toHaveFocus();
  });

  it('the Delete CV dialog takes focus inside, and Cancel returns it to the opener', async () => {
    serve();
    renderWithRouter(`${ACCOUNT}/cvs/cv-1`, { queryClient: createAppQueryClient() });
    await click(DELETE_CV_LABEL);
    const dialog = await screen.findByRole('alertdialog');
    expect(dialog).toContainElement(document.activeElement as HTMLElement);

    await userEvent.setup().click(within(dialog).getByRole('button', { name: CANCEL_LABEL }));

    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: DELETE_CV_LABEL })).toHaveFocus();
  });

  it('a read page takes no focus on load', async () => {
    serve();
    renderWithRouter(`${ACCOUNT}/cvs/cv-1`, { queryClient: createAppQueryClient() });

    await screen.findByRole('heading', { level: 1, name: 'Backend roles' });
    expect(document.body).toHaveFocus();
  });
});
