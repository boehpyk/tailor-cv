import { screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import {
  USER_A,
  historyPage,
  makeHistoryEntry,
  ok,
  signInAs,
  signedInRoutes,
  stubAccountFetch,
} from '@/test/accountFetch';
import { renderWithRouter } from '@/test/render';

import { HISTORY_COLUMN_LABELS } from '../historyCopy';

/**
 * T31 (test-after) — web-design-refresh AC-27 / OQ-14: the 4b list look is a `<ul>` of rows, never a
 * `<table>`, and its column header is decoration hidden from assistive technology.
 *
 * **Observed red, 2026-10-27, restored byte-exact:** with `aria-hidden` removed from the header row in
 * `HistoryPage.tsx`, the header test failed (`expected undefined to be defined`; 1 failed / 1 passed).
 */

beforeEach(() => {
  signInAs(USER_A);
  stubAccountFetch({
    ...signedInRoutes(USER_A),
    'GET /api/me/tailoring-runs': ok(
      historyPage([makeHistoryEntry({ id: 'a' }), makeHistoryEntry({ id: 'b' })], null),
    ),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('/history layout markup — AC-27, OQ-14', () => {
  it('is a list of rows and renders no table', async () => {
    const { container } = renderWithRouter('/history');
    await screen.findAllByRole('listitem');

    expect(container.querySelector('table')).toBeNull();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByRole('columnheader')).not.toBeInTheDocument();
    const list = screen.getByRole('main').querySelector('ul');
    expect(list).not.toBeNull();
    expect(within(list as HTMLElement).getAllByRole('listitem')).toHaveLength(2);
  });

  it('hides the column-label header row from assistive technology', async () => {
    const { container } = renderWithRouter('/history');
    await screen.findAllByRole('listitem');

    const header = [...container.querySelectorAll('[aria-hidden="true"]')].find((el) =>
      HISTORY_COLUMN_LABELS.every((label) => el.textContent.includes(label)),
    );
    expect(header).toBeDefined();
    expect(header?.closest('ul')).toBeNull();
  });
});
