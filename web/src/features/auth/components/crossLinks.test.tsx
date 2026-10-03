import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CHECK_EMAIL_HEADING } from '@/features/accountMail/accountMailCopy';
import { __resetForTests, authStore } from '@/features/auth/authStore';
import { ok, stubAccountFetch } from '@/test/accountFetch';
import { jsonResponse, makeRun } from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

/**
 * **Amended at slice 2.5's T37 RED** (AC-45, AC-50): registering answers 202 and signs nobody in,
 * so the two tests that ended on "registering lands on `next`" changed meaning. They now end on
 * "Check your email" and carry `next` through *Already confirmed? Log in* to `/login`, where
 * `safeNext` still judges it on arrival (the open-redirect assertion moved there, not away). Every
 * cross-link assertion above them is untouched.
 *
 * T30 RED — AC-36: `next` survives the `/login` ↔ `/register` cross-links, mounted through the REAL
 * route table (a bare page on its own `MemoryRouter` cannot show a navigation from one page to the
 * other). 2.1's open-redirect tests (`safeNext.test.ts`, the two pages' own `next` tests) are not
 * touched: this file only adds the cross-links and the round trip.
 *
 * `/login`'s *Create an account* link already forwards the query string; `/register` has no link
 * back to `/login` at all, so every "vice versa" case is red on a missing link.
 */

const NEXT = '/runs/run-1/cv';
const ENCODED_NEXT = '%2Fruns%2Frun-1%2Fcv';

const AUTHENTICATED = {
  access_token: 'token-abc',
  token_type: 'Bearer' as const,
  expires_in: 900,
  user: { id: 'user-1', email: 'alex@example.com', created_at: '2026-09-23T10:00:00Z' },
};

function routes(extra: Record<string, () => Response | Promise<Response>> = {}) {
  return stubAccountFetch({
    'GET /api/base-cvs': ok({ items: [] }),
    'GET /api/tailoring-runs': ok({ items: [] }),
    'GET /api/tailoring-runs/:id': () =>
      jsonResponse(200, makeRun({ id: 'run-1', status: 'queued' })),
    'GET /api/auth/me': ok(AUTHENTICATED.user),
    // Where an unsafe `next` falls back to (`/`), signed in: the account workspace.
    'GET /api/me/base-cvs': ok({ items: [] }),
    'GET /api/me/job-postings': ok({ items: [] }),
    'GET /api/me/tailoring-runs': ok({ items: [], next_cursor: null }),
    ...extra,
  });
}

function loginRegion(): HTMLElement {
  return screen.getByRole('region', { name: 'Log in' });
}

function registerRegion(): HTMLElement {
  return screen.getByRole('region', { name: 'Create an account' });
}

function signInLinkOnRegister(): HTMLElement {
  return within(registerRegion()).getByRole('link', { name: /^(sign in|log in)$/i });
}

beforeEach(() => {
  __resetForTests();
  authStore.signOut('expired');
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  __resetForTests();
});

describe('The login ↔ register cross-links keep `next` (AC-36)', () => {
  it('/login?next=X: the Create an account link is /register?next=X', async () => {
    routes();

    renderWithRouter(`/login?next=${ENCODED_NEXT}`);

    expect(
      within(await screen.findByRole('region', { name: 'Log in' })).getByRole('link', {
        name: 'Create an account',
      }),
    ).toHaveAttribute('href', `/register?next=${ENCODED_NEXT}`);
  });

  it('/register?next=X: the way back to log in is /login?next=X', async () => {
    routes();

    renderWithRouter(`/register?next=${ENCODED_NEXT}`);

    await screen.findByRole('region', { name: 'Create an account' });
    expect(signInLinkOnRegister()).toHaveAttribute('href', `/login?next=${ENCODED_NEXT}`);
  });

  it('without a next, neither link invents one', async () => {
    routes();
    const { router } = renderWithRouter('/login');
    await screen.findByRole('region', { name: 'Log in' });
    expect(within(loginRegion()).getByRole('link', { name: 'Create an account' })).toHaveAttribute(
      'href',
      '/register',
    );

    fireEvent.click(within(loginRegion()).getByRole('link', { name: 'Create an account' }));
    await screen.findByRole('region', { name: 'Create an account' });

    expect(router.state.location.pathname).toBe('/register');
    expect(signInLinkOnRegister()).toHaveAttribute('href', '/login');
  });

  it('the round trip: login → register → login → register keeps the same next, then registering shows Check your email whose Log in link still carries it', async () => {
    const fetch = routes({ 'POST /api/auth/register': () => new Response(null, { status: 202 }) });
    const { router } = renderWithRouter(`/login?next=${ENCODED_NEXT}`);
    await screen.findByRole('region', { name: 'Log in' });

    fireEvent.click(within(loginRegion()).getByRole('link', { name: 'Create an account' }));
    await screen.findByRole('region', { name: 'Create an account' });
    expect(router.state.location.pathname).toBe('/register');
    expect(new URLSearchParams(router.state.location.search).get('next')).toBe(NEXT);

    fireEvent.click(signInLinkOnRegister());
    await screen.findByRole('region', { name: 'Log in' });
    expect(router.state.location.pathname).toBe('/login');
    expect(new URLSearchParams(router.state.location.search).get('next')).toBe(NEXT);

    fireEvent.click(within(loginRegion()).getByRole('link', { name: 'Create an account' }));
    await screen.findByRole('region', { name: 'Create an account' });
    fireEvent.change(screen.getByLabelText(/email/i), { target: { value: 'alex@example.com' } });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: 'a very long real password' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^create account$/i }));

    await screen.findByRole('heading', { name: CHECK_EMAIL_HEADING });
    expect(router.state.location.pathname).toBe('/register');
    expect(within(screen.getByRole('main')).getByRole('link', { name: /log in/i })).toHaveAttribute(
      'href',
      `/login?next=${ENCODED_NEXT}`,
    );
    expect(fetch.calls.filter((call) => call.path === '/api/auth/register')).toHaveLength(1);
  });

  it('an unsafe next is carried verbatim by every link and still judged by safeNext on arrival, which is now at login', async () => {
    routes({
      'POST /api/auth/register': () => new Response(null, { status: 202 }),
      'POST /api/auth/login': () => jsonResponse(200, AUTHENTICATED),
    });
    const { router } = renderWithRouter('/login?next=%2F%2Fevil.example');
    await screen.findByRole('region', { name: 'Log in' });

    fireEvent.click(within(loginRegion()).getByRole('link', { name: 'Create an account' }));
    await screen.findByRole('region', { name: 'Create an account' });
    // The link on the way back proves the value travelled; the guard is what decides on arrival.
    expect(new URLSearchParams(router.state.location.search).get('next')).toBe('//evil.example');
    fireEvent.change(screen.getByLabelText(/email/i), { target: { value: 'alex@example.com' } });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: 'a very long real password' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^create account$/i }));
    await screen.findByRole('heading', { name: CHECK_EMAIL_HEADING });
    expect(router.state.location.pathname).toBe('/register');

    fireEvent.click(within(screen.getByRole('main')).getByRole('link', { name: /log in/i }));
    await screen.findByRole('region', { name: 'Log in' });
    expect(new URLSearchParams(router.state.location.search).get('next')).toBe('//evil.example');
    fireEvent.change(screen.getByLabelText(/email/i), { target: { value: 'alex@example.com' } });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: 'a very long real password' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^log in$/i }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/');
    });
    expect(router.state.location.pathname).not.toContain('evil');
  });
});
