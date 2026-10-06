import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests, authStore } from '@/features/auth/authStore';
import { ConfirmDeleteDialog } from '@/features/savedCvs/components/ConfirmDeleteDialog';
import {
  USER_A,
  bearerFor,
  callsTo,
  hang,
  historyPage,
  makeHistoryEntry,
  ok,
  signInAs,
  signedInRoutes,
  status,
  stubAccountFetch,
} from '@/test/accountFetch';
import { jsonResponse } from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

import type { RouteHandler } from '@/test/accountFetch';

/**
 * T30 RED — the history page (AC-44, H-59), deleting an entry (AC-45, H-58) and the header's
 * History link. Mounted at `/history` through the real route table (`RequireAuth` + `AccountScope`),
 * signed in, every request recorded.
 *
 * Against T29's skeleton `useHistory` is disabled and its rows are placeholders, so every
 * assertion is red on the missing behaviour, never on an import. The one test green on arrival is
 * the header link's absence for a guest (paired with its red positive).
 */

const DELETE_MESSAGE =
  "Delete this tailored application? This removes its tailored CV and cover letter, any files you exported, and the job posting if nothing else uses it, and its card on your board. This can't be undone.";

function history(pages: Record<string, RouteHandler>) {
  return stubAccountFetch({ ...signedInRoutes(USER_A), ...pages });
}

function rowContaining(text: string): HTMLElement {
  const row = screen.getAllByRole('listitem').find((item) => item.textContent.includes(text));
  if (row === undefined) {
    throw new Error(`no history row contains ${JSON.stringify(text)}`);
  }
  return row;
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  __resetForTests();
});

// --- AC-44: the four states ------------------------------------------------------------------------

describe('HistoryPage — states (AC-44)', () => {
  it('loading: "Loading your history…" as a status', async () => {
    history({ 'GET /api/me/tailoring-runs': hang });

    renderWithRouter('/history');

    expect(await screen.findByText('Loading your history…')).toHaveAttribute('role', 'status');
  });

  it('error: "Couldn\'t load your history" with Retry, never the empty state; Retry asks again', async () => {
    const fetch = history({
      'GET /api/me/tailoring-runs': (_call, n) =>
        n === 1
          ? jsonResponse(503, { error: { code: 'service_unavailable', message: 'down' } })
          : jsonResponse(200, historyPage([makeHistoryEntry()])),
    });
    const user = userEvent.setup();

    renderWithRouter('/history');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent("Couldn't load your history");
    expect(screen.queryByText('Nothing tailored yet')).not.toBeInTheDocument();
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Senior Platform Engineer')).toBeInTheDocument();
    expect(callsTo(fetch, 'GET', '/api/me/tailoring-runs')).toHaveLength(2);
  });

  it('empty: "Nothing tailored yet" with a link to the workspace', async () => {
    history({ 'GET /api/me/tailoring-runs': ok(historyPage([])) });

    renderWithRouter('/history');

    expect(await screen.findByText('Nothing tailored yet')).toBeInTheDocument();
    expect(screen.getAllByRole('link').some((link) => link.getAttribute('href') === '/')).toBe(
      true,
    );
  });

  it('reads /api/me/tailoring-runs with the bearer', async () => {
    const fetch = history({ 'GET /api/me/tailoring-runs': ok(historyPage([makeHistoryEntry()])) });

    renderWithRouter('/history');
    await screen.findByText('Senior Platform Engineer');

    const [read] = callsTo(fetch, 'GET', '/api/me/tailoring-runs');
    expect(read?.authorization).toBe(bearerFor(USER_A));
    expect(read?.query.get('cursor')).toBeNull();
  });
});

describe('HistoryPage — a row (AC-44)', () => {
  it('names the posting by title, the CV by label, shows Edited, and offers Open and Delete', async () => {
    history({
      'GET /api/me/tailoring-runs': ok(
        historyPage([makeHistoryEntry({ id: 'entry-1', edited: true })]),
      ),
    });

    renderWithRouter('/history');
    await screen.findByText('Senior Platform Engineer');

    const row = rowContaining('Senior Platform Engineer');
    expect(within(row).getByText('Backend roles')).toBeInTheDocument();
    expect(within(row).getByText('Edited')).toBeInTheDocument();
    const open = within(row).getByRole('link', { name: 'Open' });
    expect(open.getAttribute('href')).toMatch(/^\/history\/entry-1/);
    expect(within(row).getByRole('button', { name: 'Delete' })).toBeEnabled();
  });

  it('falls back to the preview without a title, to the filename without a label, and says "CV deleted" without a CV', async () => {
    history({
      'GET /api/me/tailoring-runs': ok(
        historyPage([
          makeHistoryEntry({
            id: 'no-title',
            posting: {
              id: 'p2',
              source: 'pasted',
              title: null,
              source_url: null,
              preview: 'A preview of the posting text',
            },
            base_cv: { id: 'cv-2', label: null, original_filename: 'plain-name.pdf' },
          }),
          makeHistoryEntry({ id: 'no-cv', base_cv: null }),
        ]),
      ),
    });

    renderWithRouter('/history');

    await screen.findByText('A preview of the posting text');
    const previewRow = rowContaining('A preview of the posting text');
    expect(within(previewRow).getByText('plain-name.pdf')).toBeInTheDocument();
    expect(screen.getByText('CV deleted')).toBeInTheDocument();
  });

  it('shows the base_cv_deleted failure in words', async () => {
    history({
      'GET /api/me/tailoring-runs': ok(
        historyPage([
          makeHistoryEntry({
            id: 'gone',
            status: 'failed',
            failure_reason: 'base_cv_deleted',
            retryable: false,
            completed_at: null,
          }),
        ]),
      ),
    });

    renderWithRouter('/history');

    expect(
      await screen.findByText('The CV this was using was deleted before tailoring started.', {
        exact: false,
      }),
    ).toBeInTheDocument();
  });
});

// --- AC-44 / H-59: Load more ------------------------------------------------------------------------

describe('HistoryPage — Load more (AC-44, H-59)', () => {
  it('appears only when there is a next page, and fetches it with the cursor', async () => {
    const fetch = history({
      'GET /api/me/tailoring-runs': (call) =>
        call.query.get('cursor') === 'cursor-2'
          ? jsonResponse(
              200,
              historyPage([
                makeHistoryEntry({
                  id: 'older',
                  posting: {
                    id: 'p9',
                    source: 'pasted',
                    title: 'Older role',
                    source_url: null,
                    preview: 'x',
                  },
                }),
              ]),
            )
          : jsonResponse(200, historyPage([makeHistoryEntry()], 'cursor-2')),
    });
    const user = userEvent.setup();

    renderWithRouter('/history');
    await screen.findByText('Senior Platform Engineer');
    await user.click(screen.getByRole('button', { name: 'Load more' }));

    expect(await screen.findByText('Older role')).toBeInTheDocument();
    expect(screen.getByText('Senior Platform Engineer')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument();
    const reads = callsTo(fetch, 'GET', '/api/me/tailoring-runs');
    expect(reads.map((call) => call.query.get('cursor'))).toEqual([null, 'cursor-2']);
  });

  it('a pending next page keeps the loaded rows and does not ask twice', async () => {
    const fetch = history({
      'GET /api/me/tailoring-runs': (call) =>
        call.query.get('cursor') === 'cursor-2'
          ? new Promise<Response>(() => undefined)
          : jsonResponse(200, historyPage([makeHistoryEntry()], 'cursor-2')),
    });

    renderWithRouter('/history');
    await screen.findByText('Senior Platform Engineer');
    const loadMore = screen.getByRole('button', { name: 'Load more' });
    fireEvent.click(loadMore);
    fireEvent.click(loadMore);

    await waitFor(() => {
      expect(callsTo(fetch, 'GET', '/api/me/tailoring-runs')).toHaveLength(2);
    });
    expect(screen.getByText('Senior Platform Engineer')).toBeInTheDocument();
  });

  it('H-59: a failed later page keeps page 1, says "Couldn\'t load more", and retries that page only', async () => {
    let olderAttempts = 0;
    const fetch = history({
      'GET /api/me/tailoring-runs': (call) => {
        if (call.query.get('cursor') !== 'cursor-2') {
          return jsonResponse(200, historyPage([makeHistoryEntry()], 'cursor-2'));
        }
        olderAttempts += 1;
        return olderAttempts === 1
          ? jsonResponse(503, { error: { code: 'service_unavailable', message: 'down' } })
          : jsonResponse(
              200,
              historyPage([
                makeHistoryEntry({
                  id: 'older',
                  posting: {
                    id: 'p9',
                    source: 'pasted',
                    title: 'Older role',
                    source_url: null,
                    preview: 'x',
                  },
                }),
              ]),
            );
      },
    });
    const user = userEvent.setup();

    renderWithRouter('/history');
    await screen.findByText('Senior Platform Engineer');
    await user.click(screen.getByRole('button', { name: 'Load more' }));

    expect(await screen.findByText("Couldn't load more", { exact: false })).toBeInTheDocument();
    expect(screen.getByText('Senior Platform Engineer')).toBeInTheDocument();
    expect(screen.queryByText("Couldn't load your history")).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Older role')).toBeInTheDocument();
    const firstPageReads = callsTo(fetch, 'GET', '/api/me/tailoring-runs').filter(
      (call) => call.query.get('cursor') === null,
    );
    expect(firstPageReads).toHaveLength(1);
  });
});

// --- The header link ----------------------------------------------------------------------------------

describe('App header — History (AC-44)', () => {
  it('links to /history when signed in', async () => {
    history({ 'GET /api/me/tailoring-runs': ok(historyPage([])) });

    renderWithRouter('/history');

    const banner = await screen.findByRole('banner');
    const link = await within(banner).findByRole('link', { name: 'History' });
    expect(link).toHaveAttribute('href', '/history');
  });

  it('has no History link for a guest (green on arrival — paired with the test above)', async () => {
    __resetForTests();
    authStore.signOut('expired');
    stubAccountFetch({
      'GET /api/base-cvs': ok({ items: [] }),
      'GET /api/job-postings': ok({ items: [] }),
      'GET /api/tailoring-runs': ok({ items: [] }),
    });

    renderWithRouter('/');

    await screen.findByRole('tab', { name: 'Base CV' });
    expect(screen.queryByRole('link', { name: 'History' })).not.toBeInTheDocument();
  });
});

// --- AC-45 / H-58: deleting an entry -----------------------------------------------------------------

function listThenWithout(entryId: string): RouteHandler {
  return (_call, n) =>
    jsonResponse(200, historyPage(n === 1 ? [makeHistoryEntry({ id: entryId })] : []));
}

async function openDeleteDialog(): Promise<HTMLElement> {
  await screen.findByText('Senior Platform Engineer');
  await userEvent
    .setup()
    .click(
      within(rowContaining('Senior Platform Engineer')).getByRole('button', { name: 'Delete' }),
    );
  return screen.findByRole('alertdialog');
}

describe('HistoryPage — deleting an entry (AC-45)', () => {
  it('asks first, in a dialog that says what goes; Escape cancels and returns focus to Delete', async () => {
    const fetch = history({ 'GET /api/me/tailoring-runs': listThenWithout('entry-1') });

    renderWithRouter('/history');
    const dialog = await openDeleteDialog();

    expect(dialog).toHaveTextContent(DELETE_MESSAGE);
    expect(dialog.contains(document.activeElement)).toBe(true);
    await userEvent.setup().keyboard('{Escape}');
    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });
    expect(document.activeElement).toBe(
      within(rowContaining('Senior Platform Engineer')).getByRole('button', { name: 'Delete' }),
    );
    expect(callsTo(fetch, 'DELETE', '/api/me/tailoring-runs/:id')).toHaveLength(0);
  });

  it('confirming sends DELETE with the bearer; the row says "Deleting…" and stays until the answer', async () => {
    const fetch = history({
      'GET /api/me/tailoring-runs': listThenWithout('entry-1'),
      'DELETE /api/me/tailoring-runs/:id': hang,
    });

    renderWithRouter('/history');
    const dialog = await openDeleteDialog();
    await userEvent.setup().click(within(dialog).getByRole('button', { name: /delete/i }));

    expect(await screen.findByText('Deleting…')).toBeInTheDocument();
    expect(screen.getByText('Senior Platform Engineer')).toBeInTheDocument();
    const [sent] = callsTo(fetch, 'DELETE', '/api/me/tailoring-runs/:id');
    expect(sent?.path).toBe('/api/me/tailoring-runs/entry-1');
    expect(sent?.authorization).toBe(bearerFor(USER_A));
  });

  it('a 204 removes the row once the list has been read again', async () => {
    history({
      'GET /api/me/tailoring-runs': listThenWithout('entry-1'),
      'DELETE /api/me/tailoring-runs/:id': () => new Response(null, { status: 204 }),
    });

    renderWithRouter('/history');
    const dialog = await openDeleteDialog();
    await userEvent.setup().click(within(dialog).getByRole('button', { name: /delete/i }));

    await waitFor(() => {
      expect(screen.queryByText('Senior Platform Engineer')).not.toBeInTheDocument();
    });
  });

  it('a 404 is "already gone": the row is removed', async () => {
    history({
      'GET /api/me/tailoring-runs': listThenWithout('entry-1'),
      'DELETE /api/me/tailoring-runs/:id': status(404, 'tailoring_run_not_found'),
    });

    renderWithRouter('/history');
    const dialog = await openDeleteDialog();
    await userEvent.setup().click(within(dialog).getByRole('button', { name: /delete/i }));

    await waitFor(() => {
      expect(screen.queryByText('Senior Platform Engineer')).not.toBeInTheDocument();
    });
    expect(screen.queryByText('Not deleted — try again')).not.toBeInTheDocument();
  });

  it('a 409 tailoring_run_in_progress keeps the row and says "Still tailoring…"', async () => {
    history({
      'GET /api/me/tailoring-runs': ok(historyPage([makeHistoryEntry()])),
      'DELETE /api/me/tailoring-runs/:id': status(409, 'tailoring_run_in_progress', 'busy', {
        status: 'running',
      }),
    });

    renderWithRouter('/history');
    const dialog = await openDeleteDialog();
    await userEvent.setup().click(within(dialog).getByRole('button', { name: /delete/i }));

    expect(
      await screen.findByText('Still tailoring — you can delete it when it finishes'),
    ).toBeInTheDocument();
    expect(screen.getByText('Senior Platform Engineer')).toBeInTheDocument();
  });

  it('a 503 keeps the row and says "Not deleted — try again"', async () => {
    history({
      'GET /api/me/tailoring-runs': ok(historyPage([makeHistoryEntry()])),
      'DELETE /api/me/tailoring-runs/:id': status(503, 'service_unavailable'),
    });

    renderWithRouter('/history');
    const dialog = await openDeleteDialog();
    await userEvent.setup().click(within(dialog).getByRole('button', { name: /delete/i }));

    expect(await screen.findByText('Not deleted — try again')).toBeInTheDocument();
    expect(screen.getByText('Senior Platform Engineer')).toBeInTheDocument();
  });

  it('H-58: a same-tick double confirm sends one DELETE', async () => {
    const fetch = history({
      'GET /api/me/tailoring-runs': ok(historyPage([makeHistoryEntry()])),
      'DELETE /api/me/tailoring-runs/:id': hang,
    });

    renderWithRouter('/history');
    const dialog = await openDeleteDialog();
    const confirm = within(dialog).getByRole('button', { name: /delete/i });
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    await waitFor(() => {
      expect(callsTo(fetch, 'DELETE', '/api/me/tailoring-runs/:id')).toHaveLength(1);
    });
  });

  it.each(['queued', 'running'] as const)(
    'a %s entry cannot be deleted yet, and says why',
    async (runStatus) => {
      history({
        'GET /api/me/tailoring-runs': ok(
          historyPage([makeHistoryEntry({ status: runStatus, completed_at: null })]),
        ),
      });

      renderWithRouter('/history');
      await screen.findByText('Senior Platform Engineer');

      const row = rowContaining('Senior Platform Engineer');
      expect(within(row).getByRole('button', { name: 'Delete' })).toBeDisabled();
      expect(
        within(row).getByText('Still tailoring — you can delete it when it finishes'),
      ).toBeInTheDocument();
    },
  );
});

describe("2.2's saved-CV delete dialog (AC-45)", () => {
  it('now says that tailored applications made from the CV stay in the history', () => {
    render(<ConfirmDeleteDialog name="jane.pdf" onConfirm={vi.fn()} onCancel={vi.fn()} />);

    expect(screen.getByRole('alertdialog')).toHaveTextContent(
      'Tailored applications you made from it stay in your history until you delete them.',
    );
  });
});
