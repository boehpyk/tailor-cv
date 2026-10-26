import { screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import { USER_A, signInAs, signedInRoutes, stubAccountFetch } from '@/test/accountFetch';
import { renderWithRouter } from '@/test/render';

/**
 * web-design-refresh T12 (AC-10), through the real route table: the shell's landmarks and heading
 * are the same on every page — one `banner`, one `main`, one `contentinfo`, one `<h1>` named exactly
 * "TailorCraft" holding a link to `/`, and a footer that holds *System status* and the link to
 * `/welcome`. Admin pages add a second `<h1>` inside `main` (known) and are not asserted here.
 */

function expectShell(): void {
  expect(screen.getAllByRole('banner')).toHaveLength(1);
  expect(screen.getAllByRole('main')).toHaveLength(1);
  const footers = screen.getAllByRole('contentinfo');
  expect(footers).toHaveLength(1);

  const h1s = screen.getAllByRole('heading', { level: 1 });
  expect(h1s).toHaveLength(1);
  expect(h1s[0]).toHaveAccessibleName('TailorCraft');
  expect(within(h1s[0] as HTMLElement).getByRole('link')).toHaveAttribute('href', '/');

  const footer = footers[0] as HTMLElement;
  expect(within(footer).getByRole('heading', { name: 'System status' })).toBeInTheDocument();
  expect(within(footer).getByRole('link', { name: 'How TailorCraft works' })).toHaveAttribute(
    'href',
    '/welcome',
  );
}

function hangingFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => undefined)),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('shell landmarks (AC-10)', () => {
  describe('signed out', () => {
    beforeEach(() => {
      __resetForTests();
      authStore.signOut('expired');
      hangingFetch();
    });

    it.each(['/', '/login', '/runs/some-run-id/cv'])('%s has the shell landmarks', (path) => {
      renderWithRouter(path);
      expectShell();
    });
  });

  it('/history (signed in) has the shell landmarks', () => {
    signInAs(USER_A);
    stubAccountFetch(signedInRoutes(USER_A));
    renderWithRouter('/history');
    expectShell();
  });
});
