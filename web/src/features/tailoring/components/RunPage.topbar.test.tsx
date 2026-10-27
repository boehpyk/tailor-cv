import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { USER_A, makeAccountRun, ok, signInAs, stubAccountFetch } from '@/test/accountFetch';
import { jsonResponse, makeRun, stubWorkspaceFetch } from '@/test/fixtures';
import { renderWithRouter } from '@/test/render';
import { boardServer } from '@/features/tracking/test/support';

/**
 * T28 — web-design-refresh AC-20/AC-23: on a succeeded run page there is exactly one export region
 * (closed and open sheet), and the top bar reads in DOM order: back link, export toggle, tablist,
 * save status. Guest and account.
 */

const REGION = { name: 'Download this document' } as const;

function stubGuestRun(): void {
  stubWorkspaceFetch({
    runDetail: {
      g1: () =>
        Promise.resolve(
          jsonResponse(
            200,
            makeRun({
              id: 'g1',
              status: 'succeeded',
              tailored_cv: 'my guest cv text',
              cover_letter: 'my guest letter',
              tailored_cv_character_count: 15,
              cover_letter_character_count: 15,
            }),
          ),
        ),
    },
    exports: { g1: () => Promise.resolve(jsonResponse(200, { items: [] })) },
  });
}

function stubAccountRun(): void {
  const server = boardServer({ 'user-a': [] });
  stubAccountFetch({
    ...server.routes(),
    'GET /api/me/tailoring-runs/:id': ok(
      makeAccountRun({
        id: 'a1',
        status: 'succeeded',
        tailored_cv: 'my tailored cv text',
        cover_letter: 'my cover letter text',
        tailored_cv_character_count: 19,
        cover_letter_character_count: 20,
        completed_at: '2026-09-20T10:00:09Z',
      }),
    ),
    'GET /api/me/tailoring-runs/a1/exports': ok({ items: [] }),
    'GET /api/me/tailoring-runs/:id/exports': ok({ items: [] }),
  });
}

function precedes(a: HTMLElement, b: HTMLElement): boolean {
  return Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

const cases = [
  ['guest', '/runs/g1/cv', stubGuestRun, 'Back to the workspace'],
  ['account', '/history/a1/cv', stubAccountRun, 'Back to your history'],
] as const;

describe.each(cases)('succeeded %s run page', (_scope, path, stub, backName) => {
  it('has exactly one export region, with the sheet closed and open', async () => {
    stub();
    renderWithRouter(path);

    await screen.findByRole('tablist', { name: 'Your documents' });
    expect(screen.getAllByRole('region', REGION)).toHaveLength(1);

    await userEvent.click(screen.getByRole('button', { name: 'Download' }));
    expect(screen.getByRole('button', { name: 'Download' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(screen.getAllByRole('region', REGION)).toHaveLength(1);
  });

  it('reads back link, export toggle, tablist, save status in DOM order', async () => {
    stub();
    renderWithRouter(path);

    const tablist = await screen.findByRole('tablist', { name: 'Your documents' });
    const back = screen.getByRole('link', { name: backName });
    const toggle = screen.getByRole('button', { name: 'Download' });
    await waitFor(() => {
      expect(screen.getAllByRole('status').some((s) => precedes(tablist, s))).toBe(true);
    });
    const save = screen.getAllByRole('status').find((s) => precedes(tablist, s));

    expect(precedes(back, toggle)).toBe(true);
    expect(precedes(toggle, tablist)).toBe(true);
    expect(save).toBeDefined();
  });
});

describe('the editor toolbar', () => {
  it('is named Formatting', async () => {
    stubGuestRun();
    renderWithRouter('/runs/g1/cv');
    await screen.findByRole('tablist', { name: 'Your documents' });
    expect((await screen.findAllByRole('toolbar', { name: 'Formatting' })).length).toBeGreaterThan(
      0,
    );
  });
});
