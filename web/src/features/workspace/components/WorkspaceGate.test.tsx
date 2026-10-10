import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import {
  USER_A,
  callsTo,
  ok,
  signInAs,
  signedInRoutes,
  status,
  stubAccountFetch,
} from '@/test/accountFetch';
import { renderWithRouter } from '@/test/render';

/**
 * T30 RED — AC-38 (the `/` gate follows the auth state), H-56 and AC-52 (a `/me` failure is an
 * error, never an endless loading line). Written from the spec and technical plan §7's state table,
 * mounted through the real route table so the gate is exercised exactly where a visitor meets it.
 *
 * Against T29's skeleton the gate's four branches exist but say "(skeleton)", the account workspace
 * is a placeholder paragraph and `AccountScope`'s error has no Retry — so every red below is an
 * assertion about copy or behaviour, never an import. The `anonymous` branch is 2.2's guest
 * workspace, already real: its test is green on arrival, the regression guard for the move.
 */

const ACCOUNT_PROMISE =
  "You're signed in, so what you tailor here is saved to your history — the job posting, the tailored CV and cover letter, and any files you export — until you delete it. The AI provider sees your CV's text and the job posting when you tailor. The person who runs TailorCraft can read what is stored here — CVs, job postings and tailored documents — to operate and support the service.";

/** Everything the account workspace reads, answered empty, so only the gate is under test. */
function accountWorkspaceRoutes() {
  return {
    ...signedInRoutes(USER_A),
    'GET /api/me/base-cvs': ok({ items: [] }),
    'GET /api/me/job-postings': ok({ items: [] }),
    'GET /api/me/tailoring-runs': ok({ items: [], next_cursor: null }),
  };
}

function typeableControls(container: HTMLElement): number {
  return container.querySelectorAll('input, textarea, [contenteditable="true"]').length;
}

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('WorkspacePage — the / gate (AC-38)', () => {
  it('H-56 booting: one status line, "Checking your account…", and nothing typeable mounted', () => {
    stubAccountFetch({});

    const { container } = renderWithRouter('/');

    const main = container.querySelector('main') ?? container;
    expect(screen.getByText('Checking your account…')).toHaveAttribute('role', 'status');
    expect(typeableControls(main)).toBe(0);
  });

  it('unavailable: "Couldn\'t check whether you\'re signed in" with Retry, and neither workspace', async () => {
    const fetch = stubAccountFetch({
      'POST /api/auth/refresh': status(503, 'service_unavailable'),
    });
    await authStore.bootstrap();
    expect(authStore.getSnapshot().status).toBe('unavailable');

    renderWithRouter('/');

    expect(await screen.findByText("Couldn't check whether you're signed in")).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Base CV' })).not.toBeInTheDocument();
    expect(screen.queryByText(ACCOUNT_PROMISE)).not.toBeInTheDocument();

    const refreshesBefore = callsTo(fetch, 'POST', '/api/auth/refresh').length;
    await userEvent.setup().click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => {
      expect(callsTo(fetch, 'POST', '/api/auth/refresh').length).toBe(refreshesBefore + 1);
    });
  });

  it("anonymous: the guest workspace (green on arrival — 2.2's workspace, moved)", async () => {
    stubAccountFetch({
      'GET /api/base-cvs': ok({ items: [] }),
      'GET /api/job-postings': ok({ items: [] }),
      'GET /api/tailoring-runs': ok({ items: [] }),
    });
    authStore.signOut('expired');

    renderWithRouter('/');

    expect(await screen.findByRole('tab', { name: 'Base CV' })).toBeInTheDocument();
    expect(screen.queryByText(ACCOUNT_PROMISE)).not.toBeInTheDocument();
  });

  it('authenticated: the account workspace, stating what is kept (AC-49), and no guest tabs', async () => {
    stubAccountFetch(accountWorkspaceRoutes());
    signInAs(USER_A);

    renderWithRouter('/');

    expect(await screen.findByText(ACCOUNT_PROMISE)).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Base CV' })).not.toBeInTheDocument();
  });

  it('signing out mid-page swaps in the guest workspace with no further request to any /api/me/ route', async () => {
    const fetch = stubAccountFetch({
      ...accountWorkspaceRoutes(),
      'GET /api/base-cvs': ok({ items: [] }),
      'GET /api/job-postings': ok({ items: [] }),
    });
    signInAs(USER_A);
    renderWithRouter('/');
    expect(await screen.findByText(ACCOUNT_PROMISE)).toBeInTheDocument();
    const meCallsBefore = fetch.calls.filter((call) => call.path.startsWith('/api/me/')).length;
    expect(meCallsBefore).toBeGreaterThan(0);

    act(() => {
      authStore.signOut('expired');
    });

    expect(await screen.findByRole('tab', { name: 'Base CV' })).toBeInTheDocument();
    expect(screen.queryByText(ACCOUNT_PROMISE)).not.toBeInTheDocument();
    const meCallsAfter = fetch.calls.filter((call) => call.path.startsWith('/api/me/')).length;
    expect(meCallsAfter).toBe(meCallsBefore);
  });
});

describe('WorkspacePage — a /me failure (AC-52)', () => {
  it("shows the account workspace's error state with Retry — never an endless loading line — and Retry asks /me again", async () => {
    const fetch = stubAccountFetch({
      ...accountWorkspaceRoutes(),
      'GET /api/auth/me': status(503, 'service_unavailable'),
    });
    signInAs(USER_A);

    renderWithRouter('/');

    // `useCurrentUser` retries a 5xx once, with TanStack's one-second backoff: wait past it.
    const alert = await screen.findByRole('alert', {}, { timeout: 3000 });
    const retry = await screen.findByRole('button', { name: 'Retry' }, { timeout: 3000 });
    expect(alert).toBeInTheDocument();
    expect(screen.queryByText(ACCOUNT_PROMISE)).not.toBeInTheDocument();
    const meBefore = callsTo(fetch, 'GET', '/api/auth/me').length;
    await userEvent.setup().click(retry);
    await waitFor(() => {
      expect(callsTo(fetch, 'GET', '/api/auth/me').length).toBeGreaterThan(meBefore);
    });
  });
});
