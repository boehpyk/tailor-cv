import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { saveBlob } from '@/features/export/saveBlob';
import { __resetForTests } from '@/features/auth/authStore';
import { createAppQueryClient } from '@/queryClient';
import { callsTo, signInAs, stubAccountFetch } from '@/test/accountFetch';
import { ADMIN, SESSION_ID, guestRoutes, guestWorld } from '@/test/adminGuestsFetch';
import { renderWithRouter } from '@/test/render';

import type { GuestWorld } from '@/test/adminGuestsFetch';
import type { RouteHandler } from '@/test/accountFetch';

vi.mock('@/features/export/saveBlob', () => ({ saveBlob: vi.fn() }));

/**
 * T21 RED — slice 4.4, AC-35: 4.3's three read pages for a guest subject, at
 * /admin/guests/:sessionId/{cvs,runs,postings}/:id. The 404 `guest_session_not_found` rows fail
 * against the skeleton (ContentPage words only user_not_found); the rest are regression guards for
 * the subject-aware pages T18 already threaded, and pass.
 */

beforeEach(() => {
  __resetForTests();
  vi.mocked(saveBlob).mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as unknown as Record<string, unknown>).__pwned;
});

const SLOW = { timeout: 4000 };
const BASE = `/admin/guests/${SESSION_ID}`;
const API = `/api/admin/guest-sessions/${SESSION_ID}`;
const GONE_SENTENCE =
  'This guest session no longer exists — it expired, was kept in an account, or was deleted.';
const MARKUP = '<img src=x onerror="window.__pwned=1"> and **bold**';

function open(
  path: string,
  w: GuestWorld = guestWorld(),
  overrides: Record<string, RouteHandler> = {},
) {
  const fetch = stubAccountFetch(guestRoutes(w, overrides));
  signInAs(ADMIN);
  const view = renderWithRouter(path, { queryClient: createAppQueryClient() });
  return { fetch, ...view };
}

function expectLiteral(container: HTMLElement, block: HTMLElement): void {
  expect(block).toHaveClass('whitespace-pre-wrap');
  expect(container.querySelector('img')).toBeNull();
  expect(container.querySelector('strong')).toBeNull();
  expect((window as unknown as Record<string, unknown>).__pwned).toBeUndefined();
}

describe('a guest session’s CV page — AC-35', () => {
  it('reads the guest URL with the admin bearer and shows the body as a literal text node', async () => {
    const { fetch, container } = open(`${BASE}/cvs/cv-1`, guestWorld({ cvBody: MARKUP }));

    expectLiteral(container, await screen.findByText(MARKUP));
    const calls = callsTo(fetch, 'GET', `${API}/base-cvs/cv-1`);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.authorization).toMatch(/^Bearer /);
  });

  it('has no Delete CV button, and links back to the session', async () => {
    open(`${BASE}/cvs/cv-1`);

    await screen.findByText('Guest CV text.');
    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument();
    expect(screen.getByRole('main').querySelector(`a[href="${BASE}"]`)).not.toBeNull();
  });

  it('404 guest_session_not_found: the gone sentence, not the app’s not-found page', async () => {
    open(`${BASE}/cvs/cv-1`, guestWorld({ gone: true }));

    expect(await screen.findByText(GONE_SENTENCE)).toBeVisible();
    expect(screen.queryByText("We couldn't find that page.")).not.toBeInTheDocument();
  });
});

describe('a guest session’s run page — AC-35', () => {
  it('shows both documents as literal text, downloads through the guest URLs, and has no delete button', async () => {
    const w = guestWorld();
    w.run = { ...w.run, tailored_cv: MARKUP, cover_letter: 'Cover <b>letter</b>' };
    const { fetch, container } = open(`${BASE}/runs/run-1`, w);

    expectLiteral(container, await screen.findByText(MARKUP));
    expect(screen.getByText('Cover <b>letter</b>')).toBeVisible();
    expect(container.querySelector('b')).toBeNull();
    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument();

    const user = userEvent.setup();
    const buttons = screen.getAllByRole('button', { name: 'Download .md' });
    await waitFor(() => {
      expect(buttons[0]).toBeEnabled();
    });
    const first = buttons[0];
    if (first === undefined) throw new Error('no download button');
    await user.click(first);
    await waitFor(() => {
      expect(
        callsTo(fetch, 'GET', `${API}/tailoring-runs/run-1/documents/cv/download`),
      ).toHaveLength(1);
    });
    expect(
      callsTo(fetch, 'GET', `${API}/tailoring-runs/run-1/documents/cv/download`)[0]?.query.get(
        'format',
      ),
    ).toBe('md');
    await waitFor(() => {
      expect(saveBlob).toHaveBeenCalledTimes(1);
    });
  });

  it('an export file downloads through the guest export-file URL', async () => {
    const { fetch } = open(`${BASE}/runs/run-1`);

    const download = await screen.findByRole('button', { name: /^Download(?! \.)/ });
    await waitFor(() => {
      expect(download).toBeEnabled();
    });
    await userEvent.setup().click(download);

    await waitFor(() => {
      expect(callsTo(fetch, 'GET', `${API}/export-jobs/job-1/file`)).toHaveLength(1);
    });
  });

  it('404 guest_session_not_found: the gone sentence', async () => {
    open(`${BASE}/runs/run-1`, guestWorld({ gone: true }));

    expect(await screen.findByText(GONE_SENTENCE)).toBeVisible();
    expect(screen.queryByText("We couldn't find that page.")).not.toBeInTheDocument();
  });
});

describe('a guest session’s posting page — AC-35', () => {
  it('shows the posting text as a literal text node', async () => {
    const { fetch, container } = open(
      `${BASE}/postings/posting-1`,
      guestWorld({ postingBody: MARKUP }),
    );

    expectLiteral(container, await screen.findByText(MARKUP));
    expect(callsTo(fetch, 'GET', `${API}/job-postings/posting-1`)).toHaveLength(1);
    expect(within(screen.getByRole('main')).queryByRole('button', { name: /delete/i })).toBeNull();
  });

  it('404 guest_session_not_found: the gone sentence', async () => {
    open(`${BASE}/postings/posting-1`, guestWorld({ gone: true }));

    expect(await screen.findByText(GONE_SENTENCE, {}, SLOW)).toBeVisible();
    expect(screen.queryByText("We couldn't find that page.")).not.toBeInTheDocument();
  });
});
