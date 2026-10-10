import { screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { USER_A, signInAs, signedInRoutes, stubAccountFetch } from '@/test/accountFetch';
import { renderWithRouter } from '@/test/render';

import { LAZY_LOADING } from '../lazyCopy';

/** T22 — AC-33: while the chunk is in flight the fallback is a `role="status"` "Loading…". */
vi.mock('./AdminPage', () => new Promise(() => undefined));

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('/admin while its chunk loads — AC-33', () => {
  it('shows a status "Loading…" and no alert', async () => {
    stubAccountFetch(signedInRoutes(USER_A));
    signInAs(USER_A);

    renderWithRouter('/admin');

    expect(await screen.findByText(LAZY_LOADING)).toHaveAttribute('role', 'status');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
