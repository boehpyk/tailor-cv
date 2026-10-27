import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LAZY_CHUNK_FAILED, LAZY_RELOAD_LABEL } from '@/components/ui/lazyCopy';
import { __resetForTests } from '@/features/auth/authStore';
import { renderWithRouter } from '@/test/render';

/** T34 — AC-31: the marketing chunk fails to load; the page must never be blank. */
vi.mock('./components/MarketingPage', () => {
  throw new Error('Failed to fetch dynamically imported module');
});

beforeEach(() => {
  __resetForTests();
  vi.stubGlobal('fetch', vi.fn());
  // React logs the boundary's caught error; it is the expected path here.
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('/welcome when its chunk fails to load — AC-31', () => {
  it('shows the alert and a Reload button, not a blank page', async () => {
    renderWithRouter('/welcome');

    expect(await screen.findByRole('alert')).toHaveTextContent(LAZY_CHUNK_FAILED);
    expect(screen.getByRole('button', { name: LAZY_RELOAD_LABEL })).toBeInTheDocument();
  });

  it('Reload does a full location.reload()', async () => {
    const reload = vi.fn();
    vi.stubGlobal('location', { reload });

    renderWithRouter('/welcome');
    await userEvent.setup().click(await screen.findByRole('button', { name: LAZY_RELOAD_LABEL }));

    expect(reload).toHaveBeenCalledTimes(1);
  });
});
