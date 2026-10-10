import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';

import { USER_A, hang, ok, signInAs, stubAccountFetch } from '@/test/accountFetch';

import { __resetForTests, authStore } from '../authStore';
import { AuthStatus } from './AuthStatus';

import type { User } from '../types';

/**
 * T20 RED — slice 4.1, AC-30: the header's Admin link appears iff `user.role === 'admin'`, between
 * Board and Account. Written from the spec; every absence below is paired with a positive control
 * (the Account link is there) so a component rendering nothing cannot satisfy it.
 */

const ADMIN: User = { ...USER_A, id: 'admin-1', email: 'root@example.com', role: 'admin' };

function renderStatus() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AuthStatus />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AuthStatus — the Admin link (AC-30)', () => {
  it('an admin sees an Admin link to /admin, between Board and Account', async () => {
    stubAccountFetch({ 'GET /api/auth/me': ok(ADMIN) });
    signInAs(ADMIN);

    renderStatus();

    const link = await screen.findByRole('link', { name: 'Admin' });
    expect(link).toHaveAttribute('href', '/admin');
    const names = screen
      .getAllByRole('link')
      .map((a) => a.textContent)
      .filter((text) => text === 'Board' || text === 'Admin' || text === 'Account');
    expect(names).toEqual(['Board', 'Admin', 'Account']);
  });

  it('a plain user sees no Admin link', async () => {
    stubAccountFetch({ 'GET /api/auth/me': ok(USER_A) });
    signInAs(USER_A);

    renderStatus();

    expect(await screen.findByText(USER_A.email)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Account' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Admin' })).not.toBeInTheDocument();
  });

  it('while the profile is still loading (user undefined) there is no Admin link', () => {
    stubAccountFetch({ 'GET /api/auth/me': hang });
    signInAs(ADMIN);

    renderStatus();

    expect(screen.getByRole('link', { name: 'Account' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Admin' })).not.toBeInTheDocument();
  });

  it('anonymous visitors see no Admin link', () => {
    stubAccountFetch({});
    authStore.signOut('logged_out');

    renderStatus();

    expect(screen.getByRole('link', { name: 'Log in' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Admin' })).not.toBeInTheDocument();
  });
});
