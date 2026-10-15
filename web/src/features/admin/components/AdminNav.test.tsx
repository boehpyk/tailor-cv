import { screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import { signInAs, stubAccountFetch } from '@/test/accountFetch';
import { contentRoutes, world } from '@/test/adminContentFetch';
import { ADMIN, SESSION_ID, guestRoutes, guestWorld } from '@/test/adminGuestsFetch';
import { renderWithRouter } from '@/test/render';

/**
 * T21 RED — slice 4.4, AC-31: every /admin page carries a nav of Users · Guests · Storage with
 * aria-current="page" on the current area. Against T20's skeleton (AdminNav renders null) each
 * fails on the absent nav.
 */

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function adminNav(path: string): Promise<HTMLElement> {
  stubAccountFetch({ ...contentRoutes(world()), ...guestRoutes(guestWorld()) });
  signInAs(ADMIN);
  renderWithRouter(path, { queryClient: createAppQueryClient() });
  // The app shell has its own nav (with an Admin link): ours is the one holding "Guests".
  const link = await screen.findByRole('link', { name: 'Guests' });
  const nav = link.closest('nav');
  if (nav === null) throw new Error('the Guests link is not inside a nav');
  return nav;
}

const current = (nav: HTMLElement): string[] =>
  within(nav)
    .getAllByRole('link')
    .filter((link) => link.getAttribute('aria-current') === 'page')
    .map((link) => link.textContent);

describe('the admin nav — AC-31', () => {
  it('links Users, Guests and Storage to their areas', async () => {
    const nav = await adminNav('/admin');

    expect(within(nav).getByRole('link', { name: 'Users' })).toHaveAttribute('href', '/admin');
    expect(within(nav).getByRole('link', { name: 'Guests' })).toHaveAttribute(
      'href',
      '/admin/guests',
    );
    expect(within(nav).getByRole('link', { name: 'Storage' })).toHaveAttribute(
      'href',
      '/admin/storage',
    );
  });

  it.each([
    ['/admin', 'Users'],
    ['/admin/users/u-ann', 'Users'],
    ['/admin/users/u-ann/cvs/cv-1', 'Users'],
    ['/admin/guests', 'Guests'],
    [`/admin/guests/${SESSION_ID}`, 'Guests'],
    [`/admin/guests/${SESSION_ID}/cvs/cv-1`, 'Guests'],
    ['/admin/storage', 'Storage'],
  ])('at %s exactly "%s" is the current area', async (path, area) => {
    const nav = await adminNav(path);

    expect(current(nav)).toEqual([area]);
  });
});
