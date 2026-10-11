import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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
import { GUEST_HOME } from '@/test/adminFetch';
import { renderWithRouter } from '@/test/render';

import { ACCOUNT_DISABLED_NOTICE, LOGIN_ACCOUNT_DISABLED } from '../authCopy';
import { __resetForTests, authStore } from '../authStore';

/**
 * T20 RED — slice 4.2, AC-36: the disabled user's side, in the main bundle. Written from the spec;
 * against T19 `client.ts` has no `account_disabled` branch, `RequireAuth` maps no such reason and
 * the login form has no 403 copy, so each fails on an observable outcome.
 */

const disabled = status(403, 'account_disabled');

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('AC-36 (a) a bearer request answered 403 account_disabled', () => {
  function disabledOnHistory() {
    const fetch = stubAccountFetch({
      ...signedInRoutes(USER_A),
      ...GUEST_HOME,
      'GET /api/me/tailoring-runs': disabled,
    });
    signInAs(USER_A);
    const posted: unknown[] = [];
    authStore.connectChannel(
      {
        postMessage: (message: unknown) => posted.push(message),
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
      },
      () => undefined,
    );
    const view = renderWithRouter('/history', { queryClient: createAppQueryClient() });
    return { fetch, posted, ...view };
  }

  it('signs the tab out with reason account_disabled', async () => {
    disabledOnHistory();

    await waitFor(() => {
      expect(authStore.getSnapshot()).toEqual({ status: 'anonymous', reason: 'account_disabled' });
    });
  });

  it('does not try a refresh first', async () => {
    const { fetch } = disabledOnHistory();
    await waitFor(() => {
      expect(authStore.getSnapshot().status).toBe('anonymous');
    });

    expect(callsTo(fetch, 'POST', '/api/auth/refresh')).toHaveLength(0);
  });

  it('tells the other tabs on the BroadcastChannel', async () => {
    const { posted } = disabledOnHistory();

    await waitFor(() => {
      expect(posted).toEqual([{ type: 'signed-out' }]);
    });
  });
});

describe('AC-36 (b) RequireAuth with that reason', () => {
  it('goes to / with "Your account has been disabled, so you were signed out."', async () => {
    stubAccountFetch({
      ...signedInRoutes(USER_A),
      ...GUEST_HOME,
      'GET /api/me/tailoring-runs': hang,
    });
    signInAs(USER_A);
    const { router } = renderWithRouter('/history', { queryClient: createAppQueryClient() });
    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/history');
    });

    act(() => {
      authStore.signOut('account_disabled');
    });

    expect(await screen.findByText(ACCOUNT_DISABLED_NOTICE)).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/');
  });
});

describe('AC-36 (c) /login answering 403 account_disabled', () => {
  it('shows "This account has been disabled." in the form alert and keeps the typed email', async () => {
    stubAccountFetch({ 'POST /api/auth/login': disabled });
    authStore.signOut('logged_out');
    renderWithRouter('/login', { queryClient: createAppQueryClient() });

    fireEvent.change(await screen.findByLabelText(/email/i), {
      target: { value: 'gone@example.com' },
    });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: 'a very long real password' },
    });
    fireEvent.click(screen.getByRole('button', { name: /log in/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(LOGIN_ACCOUNT_DISABLED);
    expect(screen.getByLabelText(/email/i)).toHaveValue('gone@example.com');
  });
});
