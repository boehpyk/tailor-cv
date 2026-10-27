import { screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LAZY_LOADING } from '@/components/ui/lazyCopy';
import { __resetForTests } from '@/features/auth/authStore';
import { renderWithRouter } from '@/test/render';

/** T34 — AC-31: while the chunk is in flight the fallback is a `role="status"` "Loading…". */
vi.mock('./components/MarketingPage', () => new Promise(() => undefined));

beforeEach(() => {
  __resetForTests();
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('/welcome while its chunk loads — AC-31', () => {
  it('shows a status "Loading…" and no alert', async () => {
    renderWithRouter('/welcome');

    expect(await screen.findByText(LAZY_LOADING)).toHaveAttribute('role', 'status');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
