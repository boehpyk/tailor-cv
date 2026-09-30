import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import {
  USER_A,
  historyPage,
  makeHistoryEntry,
  ok,
  signInAs,
  signedInRoutes,
  status,
  stubAccountFetch,
} from '@/test/accountFetch';
import { renderWithRouter } from '@/test/render';

import {
  DELETE_ENTRY_DIALOG_TITLE,
  DELETE_ENTRY_FAILED_NOTE,
  DELETE_ENTRY_IN_PROGRESS_NOTE,
  DELETE_ENTRY_MESSAGE,
} from '../historyCopy';

import type { RouteHandler } from '@/test/accountFetch';

/**
 * T35 (test-after) — AC-50's markup and accessibility on the history page and the account workspace:
 * every control has an accessible name; an error is `role="alert"` and is linked to the control it
 * is about by `aria-describedby`; the delete dialog is named and described by its own text, traps
 * focus and returns it; and *Load more* is a button, not infinite scroll.
 *
 * **Observed red, 2026-09-30, and restored byte-exact:** with the Delete button's
 * `aria-describedby` removed from `HistoryEntryRow.tsx`, both linkage tests failed (2 failed / 4
 * passed).
 */

const TWO_ROWS = historyPage(
  [
    makeHistoryEntry({ id: 'done' }),
    makeHistoryEntry({
      id: 'busy',
      status: 'running',
      completed_at: null,
      posting: {
        id: 'p2',
        source: 'pasted',
        title: 'Running role',
        source_url: null,
        preview: 'x',
      },
    }),
  ],
  'cursor-2',
);

function stub(extra: Record<string, RouteHandler> = {}) {
  return stubAccountFetch({
    ...signedInRoutes(USER_A),
    'GET /api/me/tailoring-runs': ok(TWO_ROWS),
    ...extra,
  });
}

function rowContaining(text: string): HTMLElement {
  const row = screen.getAllByRole('listitem').find((item) => item.textContent.includes(text));
  if (row === undefined) {
    throw new Error(`no row contains ${text}`);
  }
  return row;
}

function describedBy(element: HTMLElement): HTMLElement | null {
  const id = element.getAttribute('aria-describedby');
  return id === null ? null : document.getElementById(id);
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  __resetForTests();
});

describe('History page — markup and a11y (AC-50)', () => {
  it('every button, link and tab on the page has an accessible name', async () => {
    stub();

    renderWithRouter('/history');
    await screen.findByText('Senior Platform Engineer');

    for (const role of ['button', 'link'] as const) {
      for (const control of screen.getAllByRole(role)) {
        expect(control).toHaveAccessibleName();
      }
    }
  });

  it('Load more is a real button, not a scroll trigger', async () => {
    stub();

    renderWithRouter('/history');
    const loadMore = await screen.findByRole('button', { name: 'Load more' });

    expect(loadMore.tagName).toBe('BUTTON');
    expect(loadMore).toHaveAttribute('type', 'button');
  });

  it("a running entry's disabled Delete is described by the reason it cannot be deleted", async () => {
    stub();

    renderWithRouter('/history');
    await screen.findByText('Running role');

    const del = within(rowContaining('Running role')).getByRole('button', { name: 'Delete' });
    expect(del).toBeDisabled();
    expect(describedBy(del)).toHaveTextContent(DELETE_ENTRY_IN_PROGRESS_NOTE);
  });

  it('a failed delete is an alert, linked to the Delete button by aria-describedby', async () => {
    stub({ 'DELETE /api/me/tailoring-runs/:id': status(503, 'service_unavailable') });
    const user = userEvent.setup();

    renderWithRouter('/history');
    await screen.findByText('Senior Platform Engineer');
    await user.click(
      within(rowContaining('Senior Platform Engineer')).getByRole('button', { name: 'Delete' }),
    );
    await user.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: /delete/i }),
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(DELETE_ENTRY_FAILED_NOTE);
    const del = within(rowContaining('Senior Platform Engineer')).getByRole('button', {
      name: 'Delete',
    });
    expect(describedBy(del)).toBe(alert);
  });

  it('the delete dialog is named and described by its own text, traps Tab both ways, and gives focus back', async () => {
    stub();
    const user = userEvent.setup();

    renderWithRouter('/history');
    await screen.findByText('Senior Platform Engineer');
    const opener = within(rowContaining('Senior Platform Engineer')).getByRole('button', {
      name: 'Delete',
    });
    await user.click(opener);
    const dialog = screen.getByRole('alertdialog');

    expect(dialog).toHaveAccessibleName(DELETE_ENTRY_DIALOG_TITLE);
    expect(dialog).toHaveAccessibleDescription(DELETE_ENTRY_MESSAGE);
    const buttons = within(dialog).getAllByRole('button');
    expect(buttons).toHaveLength(2);
    const [first, last] = buttons as [HTMLElement, HTMLElement];
    expect(document.activeElement).toBe(first);
    await user.tab();
    expect(document.activeElement).toBe(last);
    await user.tab();
    expect(document.activeElement).toBe(first);
    await user.tab({ shift: true });
    expect(document.activeElement).toBe(last);

    await user.keyboard('{Escape}');
    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });
    expect(document.activeElement).toBe(opener);
  });
});

describe('Account workspace — markup and a11y (AC-50)', () => {
  it('every control has an accessible name, and every section is a named region', async () => {
    stubAccountFetch({
      ...signedInRoutes(USER_A),
      'GET /api/me/base-cvs': ok({
        items: [
          {
            id: 'saved-cv-1',
            label: null,
            original_filename: 'jane.pdf',
            content_type: 'application/pdf',
            size_bytes: 2048,
            status: 'extracted',
            character_count: 512,
            failure_reason: null,
            failure_message: null,
            uploaded_at: '2026-09-20T10:00:00Z',
          },
        ],
      }),
      'GET /api/me/job-postings': ok({ items: [] }),
      'GET /api/me/tailoring-runs': ok(historyPage([])),
    });

    renderWithRouter('/');
    await screen.findByRole('radio', { name: /jane\.pdf/ });

    for (const role of ['button', 'link', 'radio', 'textbox'] as const) {
      for (const control of screen.queryAllByRole(role)) {
        expect(control).toHaveAccessibleName();
      }
    }
    const main = screen.getByRole('main');
    const regions = within(main).getAllByRole('region');
    expect(regions.length).toBeGreaterThan(0);
    for (const region of regions) {
      expect(region).toHaveAccessibleName();
    }
  });
});
