import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { USER_A, signInAs, signedInRoutes, stubAccountFetch } from '@/test/accountFetch';
import { renderWithRouter } from '@/test/render';

import { LAZY_CHUNK_FAILED, LAZY_RELOAD_LABEL } from '../lazyCopy';

/**
 * T22 — AC-33 / R-22: the admin chunk fails to load (a tab older than the last release). The
 * dynamic import is made to reject by a throwing module factory; the page must never be blank.
 */
vi.mock('./AdminPage', () => {
  throw new Error('Failed to fetch dynamically imported module');
});

beforeEach(() => {
  __resetForTests();
  // React logs the boundary's caught error; it is the expected path here.
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('/admin when its chunk fails to load — AC-33', () => {
  it('shows the alert and a Reload button, not a blank page', async () => {
    stubAccountFetch(signedInRoutes(USER_A));
    signInAs(USER_A);

    renderWithRouter('/admin');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(LAZY_CHUNK_FAILED);
    expect(screen.getByRole('button', { name: LAZY_RELOAD_LABEL })).toBeInTheDocument();
  });

  it('Reload does a full location.reload()', async () => {
    stubAccountFetch(signedInRoutes(USER_A));
    signInAs(USER_A);
    const reload = vi.fn();
    vi.stubGlobal('location', { reload });

    renderWithRouter('/admin');
    await userEvent.setup().click(await screen.findByRole('button', { name: LAZY_RELOAD_LABEL }));

    expect(reload).toHaveBeenCalledTimes(1);
  });
});
