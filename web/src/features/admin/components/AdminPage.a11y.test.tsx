import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthStatus } from '@/features/auth/components/AuthStatus';
import { __resetForTests } from '@/features/auth/authStore';
import { USER_A, ok, signInAs, hang, status, stubAccountFetch } from '@/test/accountFetch';

import { ADMIN_CHECKING, ADMIN_HEADING, ADMIN_UNAVAILABLE } from '../adminCopy';

import AdminPage from './AdminPage';

import type { User } from '@/features/auth/types';

/** T22 — AC-36: landmarks, roles and focus of the admin surface. */

const ADMIN: User = { ...USER_A, id: 'admin-1', role: 'admin' };

function client(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  });
}

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('accessibility — AC-36', () => {
  it('the Admin link sits inside nav "Account", between Board and Account', async () => {
    stubAccountFetch({ 'GET /api/auth/me': ok(ADMIN) });
    signInAs(ADMIN);
    render(
      <QueryClientProvider client={client()}>
        <MemoryRouter>
          <AuthStatus />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await screen.findByRole('link', { name: 'Admin' });
    const nav = screen.getByRole('navigation', { name: 'Account' });
    const names = within(nav)
      .getAllByRole('link')
      .map((a) => a.textContent);
    expect(names.indexOf('Admin')).toBeGreaterThan(names.indexOf('Board'));
    expect(names.indexOf('Admin')).toBe(names.indexOf('Account') - 1);
  });

  function renderPage() {
    return render(
      <QueryClientProvider client={client()}>
        <MemoryRouter>
          <AdminPage userId={ADMIN.id} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it('the shell is a region labelled by its h1', async () => {
    stubAccountFetch({ 'GET /api/admin/access': () => new Response(null, { status: 204 }) });
    renderPage();

    const region = await screen.findByRole('region', { name: ADMIN_HEADING });
    const h1 = within(region).getByRole('heading', { level: 1 });
    expect(region).toHaveAttribute('aria-labelledby', h1.id);
    expect(h1.id).not.toBe('');
  });

  it('checking is a status', async () => {
    stubAccountFetch({ 'GET /api/admin/access': hang });
    renderPage();

    expect(await screen.findByRole('status')).toHaveTextContent(ADMIN_CHECKING);
  });

  it('unavailable is an alert, and no state moves focus', async () => {
    stubAccountFetch({ 'GET /api/admin/access': status(503, 'service_unavailable') });
    const before = document.activeElement;
    renderPage();

    expect(await screen.findByRole('alert', {}, { timeout: 4000 })).toHaveTextContent(
      ADMIN_UNAVAILABLE,
    );
    expect(document.activeElement).toBe(before);
    expect(document.activeElement).toBe(document.body);
  });

  it('the shell arriving does not move focus either', async () => {
    stubAccountFetch({ 'GET /api/admin/access': () => new Response(null, { status: 204 }) });
    renderPage();

    await screen.findByRole('heading', { level: 1, name: ADMIN_HEADING });
    expect(document.activeElement).toBe(document.body);
  });
});
