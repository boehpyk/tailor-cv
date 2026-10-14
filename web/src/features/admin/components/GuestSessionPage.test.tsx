import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import { callsTo, hang, signInAs, status, stubAccountFetch } from '@/test/accountFetch';
import {
  ADMIN,
  OTHER_SESSION_ID,
  SESSION_ID,
  guestRoutes,
  guestWorld,
  sessionGone,
  sessionSummary,
} from '@/test/adminGuestsFetch';
import { makeRunSummary } from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';

import { DELETE_GUEST_FAILED, GUEST_SECTION_EMPTY, GUEST_SESSION_ERROR } from '../adminCopy';
import { guestListKey } from '../hooks/useGuestSessions';

import type { GuestWorld } from '@/test/adminGuestsFetch';
import type { RouteHandler } from '@/test/accountFetch';

/**
 * T21 RED — slice 4.4, AC-33 (one guest session) and AC-34 (delete it). Against T20's skeleton
 * (an empty div) each fails on an absent element.
 */

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const SLOW = { timeout: 4000 };
const G = '/api/admin/guest-sessions/:id';
const GONE_SENTENCE =
  'This guest session no longer exists — it expired, was kept in an account, or was deleted.';

function open(
  w: GuestWorld = guestWorld(),
  overrides: Record<string, RouteHandler> = {},
  sessionId: string = SESSION_ID,
) {
  const fetch = stubAccountFetch(guestRoutes(w, overrides));
  signInAs(ADMIN);
  const queryClient = createAppQueryClient();
  const view = renderWithRouter(`/admin/guests/${sessionId}`, { queryClient });
  return { fetch, queryClient, ...view };
}

const mainEl = (): HTMLElement => screen.getByRole('main');
const hrefIn = (href: string): Element | null => mainEl().querySelector(`a[href="${href}"]`);

describe('the guest session page — AC-33', () => {
  it('loading: a status line while the session is pending', async () => {
    const { fetch } = open(guestWorld(), { [`GET ${G}`]: hang });

    await waitFor(() => {
      expect(callsTo(fetch, 'GET', G)).toHaveLength(1);
    });
    expect(within(mainEl()).getByText('Loading guest session…')).toHaveAttribute('role', 'status');
  });

  it('the overview: one h1 "Guest session", started, expires and the counts', async () => {
    open();

    expect(await screen.findByRole('heading', { level: 1, name: 'Guest session' })).toBeVisible();
    expect(within(mainEl()).getAllByRole('heading', { level: 1 })).toHaveLength(1);
    for (const count of ['3', '4', '5', '6']) {
      expect(mainEl()).toHaveTextContent(new RegExp(`\\b${count}\\b`));
    }
    expect(mainEl()).toHaveTextContent(/\d{1,2}:\d{2}/);
  });

  it('three sections, each listing its items with a link to the read page', async () => {
    open();

    await screen.findByRole('heading', { level: 1, name: 'Guest session' });
    for (const name of ['CVs', 'Job postings', 'Tailoring runs']) {
      expect(screen.getByRole('heading', { level: 2, name })).toBeVisible();
    }
    expect(hrefIn(`/admin/guests/${SESSION_ID}/cvs/cv-1`)).not.toBeNull();
    expect(hrefIn(`/admin/guests/${SESSION_ID}/postings/posting-1`)).not.toBeNull();
    expect(hrefIn(`/admin/guests/${SESSION_ID}/runs/run-1`)).not.toBeNull();
  });

  it('a session holding nothing shows an empty sentence per section', async () => {
    const w = guestWorld({
      sessionPages: [[sessionSummary({ id: OTHER_SESSION_ID })]],
    });
    open(w, {}, OTHER_SESSION_ID);

    expect(await screen.findByText(GUEST_SECTION_EMPTY.cvs)).toBeVisible();
    expect(screen.getByText(GUEST_SECTION_EMPTY.postings)).toBeVisible();
    expect(screen.getByText(GUEST_SECTION_EMPTY.runs)).toBeVisible();
  });

  it('guest_session_not_found: the gone sentence and a link to /admin/guests', async () => {
    open(guestWorld({ gone: true }));

    expect(await screen.findByText(GONE_SENTENCE)).toBeVisible();
    expect(hrefIn('/admin/guests')).not.toBeNull();
    expect(screen.queryByText("We couldn't find that page.")).not.toBeInTheDocument();
  });

  it('a 503 is an alert with Retry, distinct from loading; Retry shows the session', async () => {
    let calls = 0;
    open(guestWorld(), {
      [`GET ${G}`]: () => {
        calls += 1;
        return calls <= 2
          ? status(503, 'service_unavailable')()
          : Response.json({
              session: sessionSummary(),
              base_cvs: [],
              job_postings: [],
              tailoring_runs: [],
            });
      },
    });

    const alert = await screen.findByRole('alert', {}, SLOW);
    expect(within(alert).getByText(GUEST_SESSION_ERROR)).toBeVisible();
    await userEvent.setup().click(within(alert).getByRole('button', { name: 'Retry' }));

    expect(await screen.findByRole('heading', { level: 1, name: 'Guest session' })).toBeVisible();
  });
});

async function openDialog(): Promise<HTMLElement> {
  const trigger = await screen.findByRole('button', { name: 'Delete guest session' });
  await waitFor(() => {
    expect(trigger).toBeEnabled();
  });
  await userEvent.setup().click(trigger);
  return screen.findByRole('alertdialog');
}

const confirmButton = (dialog: HTMLElement): HTMLElement =>
  within(dialog).getByRole('button', { name: 'Delete guest session' });

describe('delete a guest session — AC-34', () => {
  it('the dialog names the counts, the 24-hour purge and that it cannot be undone; nothing is sent yet', async () => {
    const { fetch } = open();

    const dialog = await openDialog();

    for (const count of ['3', '4', '5', '6']) {
      expect(dialog).toHaveTextContent(new RegExp(`\\b${count}\\b`));
    }
    expect(dialog).toHaveTextContent('before the 24-hour purge');
    expect(dialog).toHaveTextContent('cannot be undone');
    expect(dialog).not.toHaveTextContent('A run still in progress is discarded');
    expect(callsTo(fetch, 'DELETE', G)).toHaveLength(0);
  });

  it('a queued or running run adds "a run still in progress is discarded"', async () => {
    const w = guestWorld();
    w.content[SESSION_ID] = {
      cvs: [],
      postings: [],
      runs: [makeRunSummary({ id: 'run-q', status: 'queued' })],
    };
    open(w);

    const dialog = await openDialog();

    expect(dialog).toHaveTextContent(/a run still in progress is discarded/i);
  });

  it('Cancel sends nothing', async () => {
    const { fetch } = open();

    const dialog = await openDialog();
    await userEvent.setup().click(within(dialog).getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(callsTo(fetch, 'DELETE', G)).toHaveLength(0);
  });

  it('confirm sends one DELETE, lands on /admin/guests with the notice, and the session is gone from the list', async () => {
    const { fetch, router } = open();

    const dialog = await openDialog();
    fireEvent.click(confirmButton(dialog));

    expect(await screen.findByText('Deleted the guest session. 3 file(s) removed.')).toBeVisible();
    expect(router.state.location.pathname).toBe('/admin/guests');
    expect(callsTo(fetch, 'DELETE', G)).toHaveLength(1);
    expect(await screen.findByText('No live guest sessions.')).toBeVisible();
  });

  it('a double click on confirm sends one DELETE', async () => {
    const { fetch } = open();

    const dialog = await openDialog();
    const confirm = confirmButton(dialog);
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    await screen.findByText('Deleted the guest session. 3 file(s) removed.');
    expect(callsTo(fetch, 'DELETE', G)).toHaveLength(1);
  });

  it('files that could not be removed are counted beside the notice', async () => {
    open(guestWorld({ erasure: { files_unlinked: 1, unlink_failures: 2 } }));

    const dialog = await openDialog();
    fireEvent.click(confirmButton(dialog));

    expect(await screen.findByText('Deleted the guest session. 1 file(s) removed.')).toBeVisible();
    expect(
      screen.getByText('2 could not be removed; the orphan sweep will reclaim them.'),
    ).toBeVisible();
  });

  it('not optimistic: while the DELETE is pending the page still shows the session', async () => {
    const { fetch, router } = open(guestWorld(), { [`DELETE ${G}`]: hang });

    const dialog = await openDialog();
    fireEvent.click(confirmButton(dialog));

    await waitFor(() => {
      expect(callsTo(fetch, 'DELETE', G)).toHaveLength(1);
    });
    expect(router.state.location.pathname).toBe(`/admin/guests/${SESSION_ID}`);
    expect(screen.getByRole('heading', { level: 1, name: 'Guest session' })).toBeVisible();
    expect(screen.queryByText(/Deleted the guest session/)).not.toBeInTheDocument();
  });

  it('a 404 on the delete says the session is gone and refetches the list', async () => {
    const w = guestWorld();
    const { queryClient } = open(w, {
      [`DELETE ${G}`]: () => {
        w.gone = true;
        return sessionGone();
      },
    });
    // The list was visited before: its cached pages must be marked stale by the 404.
    await queryClient.infiniteQuery({
      queryKey: guestListKey(ADMIN.id),
      queryFn: () => ({ items: [], next_cursor: null }),
      initialPageParam: null as string | null,
    });

    const dialog = await openDialog();
    fireEvent.click(confirmButton(dialog));

    expect(await screen.findByText(GONE_SENTENCE)).toBeVisible();
    await waitFor(() => {
      expect(queryClient.getQueryState(guestListKey(ADMIN.id))?.isInvalidated).toBe(true);
    });
  });

  it('a 503 on the delete is an alert and the session page stays usable', async () => {
    open(guestWorld(), { [`DELETE ${G}`]: status(503, 'service_unavailable') });

    const dialog = await openDialog();
    fireEvent.click(confirmButton(dialog));

    expect(await screen.findByRole('alert')).toHaveTextContent(DELETE_GUEST_FAILED);
    expect(screen.getByRole('button', { name: 'Delete guest session' })).toBeEnabled();
  });
});
