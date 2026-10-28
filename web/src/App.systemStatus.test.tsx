import { act, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import { USER_A, callsTo, signInAs, signedInRoutes, stubAccountFetch } from '@/test/accountFetch';
import { ADMIN } from '@/test/adminContentFetch';
import { renderWithRouter } from '@/test/render';

/**
 * System status is an operator's view, so the shell's footer shows it to admins only. Everyone else
 * gets no heading, no section and no `/health/ready` poll: the component is not mounted at all,
 * which matters more than hiding it (a hidden poller still polls). `/health/ready` itself stays
 * public: the deploy and the box's checks read it, not this page.
 */

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

async function settle(): Promise<void> {
  await act(async () => {});
  await act(async () => {});
}

function footer(): HTMLElement {
  return screen.getByRole('contentinfo');
}

describe('System status is for admins only', () => {
  it('an admin sees it in the footer', async () => {
    signInAs(ADMIN);
    stubAccountFetch(signedInRoutes(ADMIN));
    renderWithRouter('/history');

    expect(
      await within(footer()).findByRole('heading', { name: 'System status' }),
    ).toBeInTheDocument();
  });

  it('a signed-in user does not see it, and the page never asks /health/ready', async () => {
    signInAs(USER_A);
    const fetch = stubAccountFetch(signedInRoutes(USER_A));
    renderWithRouter('/history');

    await screen.findByRole('link', { name: 'Account' });
    await settle();

    expect(within(footer()).queryByRole('heading', { name: 'System status' })).toBeNull();
    expect(callsTo(fetch, 'GET', '/health/ready')).toHaveLength(0);
  });

  it('a signed-out visitor does not see it, and the page never asks /health/ready', async () => {
    __resetForTests();
    authStore.signOut('expired');
    const fetch = stubAccountFetch({});
    renderWithRouter('/login');

    await screen.findByRole('link', { name: 'How FixCV works' });
    await settle();

    expect(within(footer()).queryByRole('heading', { name: 'System status' })).toBeNull();
    expect(callsTo(fetch, 'GET', '/health/ready')).toHaveLength(0);
  });
});
