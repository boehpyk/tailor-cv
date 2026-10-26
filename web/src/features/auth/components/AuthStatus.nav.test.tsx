import { screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createAppQueryClient } from '@/queryClient';
import { USER_A, hang, signInAs, signedInRoutes, stubAccountFetch } from '@/test/accountFetch';
import { noContent } from '@/test/adminFetch';
import { renderWithRouter } from '@/test/render';

import { __resetForTests, authStore } from '../authStore';

import type { User } from '../types';

/**
 * T9 RED — web-design-refresh, AC-8 (the nav's links) and AC-9 (`aria-current="page"`), through the
 * real route table. Written from the spec, not from `AuthStatus`: today the header has no
 * *Workspace* or *How it works* link and uses plain `Link`s, so each case fails on an absent link or
 * a missing `aria-current`, never on an import.
 */

const ADMIN: User = { ...USER_A, id: 'admin-1', email: 'root@example.com', role: 'admin' };
const RUN_ID = 'run-1';

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Every page's own data hangs: the nav is the thing under test, and it never waits on a page. */
function signedIn(user: User): void {
  stubAccountFetch({
    ...signedInRoutes(user),
    'GET /api/admin/access': noContent,
    'GET /api/admin/users': hang,
    'GET /api/me/tailoring-runs': hang,
    'GET /api/me/tailoring-runs/:id': hang,
    'GET /api/me/base-cvs': hang,
    'GET /api/me/board': hang,
  });
  signInAs(user);
}

function signedOut(): void {
  stubAccountFetch({
    'GET /api/tailoring-runs': hang,
    'GET /api/tailoring-runs/:id': hang,
    'GET /api/base-cvs': hang,
  });
  authStore.signOut('expired');
}

const open = (path: string) => renderWithRouter(path, { queryClient: createAppQueryClient() });

/** The one nav element, and the links in it that carry `aria-current="page"`. */
function currentLinks(): string[] {
  return within(screen.getByRole('navigation', { name: 'Account' }))
    .getAllByRole('link')
    .filter((link) => link.getAttribute('aria-current') === 'page')
    .map((link) => link.textContent);
}

function expectEachNameOnce(names: readonly string[]): void {
  for (const name of names) {
    expect(screen.getAllByRole('link', { name }), name).toHaveLength(1);
  }
}

describe('AC-8: the header nav, signed out', () => {
  it('shows Log in, Create account and How it works, each once, to the right paths', async () => {
    signedOut();

    open('/login');

    expect(await screen.findByRole('link', { name: 'How it works' })).toHaveAttribute(
      'href',
      '/welcome',
    );
    expect(screen.getByRole('link', { name: 'Log in' })).toHaveAttribute('href', '/login');
    expect(screen.getByRole('link', { name: 'Create account' })).toHaveAttribute(
      'href',
      '/register',
    );
    expectEachNameOnce(['Log in', 'Create account', 'How it works']);
  });
});

describe('AC-8: the header nav, signed in', () => {
  const USER_LINKS = ['Workspace', 'History', 'Board', 'Account'];

  it('a user sees Workspace, History, Board, Account, Log out and the email, and no Admin', async () => {
    signedIn(USER_A);

    open('/history');

    expect(await screen.findByRole('link', { name: 'Workspace' })).toHaveAttribute('href', '/');
    expect(screen.getByRole('link', { name: 'History' })).toHaveAttribute('href', '/history');
    expect(screen.getByRole('link', { name: 'Board' })).toHaveAttribute('href', '/board');
    expect(screen.getByRole('link', { name: 'Account' })).toHaveAttribute('href', '/account');
    expect(await screen.findByText(USER_A.email)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Log out' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Admin' })).not.toBeInTheDocument();
    expectEachNameOnce(USER_LINKS);
  });

  it('an admin also sees Admin, still once', async () => {
    signedIn(ADMIN);

    open('/history');

    expect(await screen.findByRole('link', { name: 'Admin' })).toHaveAttribute('href', '/admin');
    expectEachNameOnce([...USER_LINKS, 'Admin']);
  });

  it('Log out is absent on /account, where the page has its own (positive control: Account link)', async () => {
    signedIn(USER_A);

    open('/account');

    expect(await screen.findByRole('link', { name: 'Account' })).toBeInTheDocument();
    expect(
      within(screen.getByRole('navigation', { name: 'Account' })).queryByRole('button', {
        name: 'Log out',
      }),
    ).not.toBeInTheDocument();
  });
});

describe('AC-9: aria-current="page" marks exactly the current route', () => {
  it.each([
    ['/', 'Workspace', USER_A],
    ['/history', 'History', USER_A],
    [`/history/${RUN_ID}/cv`, 'History', USER_A],
    ['/board', 'Board', USER_A],
    ['/account', 'Account', USER_A],
    ['/admin/users', 'Admin', ADMIN],
  ] as const)('at %s only %s is current', async (path, name, user) => {
    signedIn(user);

    open(path);

    await screen.findByRole('link', { name: 'Account' });
    expect(await screen.findByRole('link', { name })).toHaveAttribute('aria-current', 'page');
    expect(currentLinks()).toEqual([name]);
  });

  it('at /runs/:id/cv a guest has no current link (positive control: the nav rendered)', async () => {
    signedOut();

    open(`/runs/${RUN_ID}/cv`);

    expect(await screen.findByRole('link', { name: 'Log in' })).toBeInTheDocument();
    expect(currentLinks()).toEqual([]);
  });
});
