import { screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { USER_A, signInAs, stubAccountFetch } from '@/test/accountFetch';
import { renderWithRouter } from '@/test/render';

import { boardServer, newClient } from './test/support';

/**
 * Slice 3.1 `/verify` r1 — the spec's Privacy section: what the board keeps, and for how long, is
 * said on the board's empty state **and** on `/account`. `/account` is where a person looks to learn
 * what the product holds about them; it names saved CVs and history, and must name the board.
 */

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('/account says what the board keeps', () => {
  it('states that board cards stay until removed, their history entry is deleted, or the account is', async () => {
    const server = boardServer({ 'user-a': [] });
    stubAccountFetch(server.routes());

    renderWithRouter('/account', { queryClient: newClient() });

    // Positive: the account page itself rendered, so an absent note is not an absent page.
    expect((await screen.findAllByText(USER_A.email)).length).toBeGreaterThan(0);
    const note = await screen.findByText(
      (_content, element) => {
        const text = element?.textContent ?? '';
        return (
          element?.children.length === 0 &&
          /board/i.test(text) &&
          /until you remove/i.test(text) &&
          /history entry/i.test(text) &&
          /delete your account/i.test(text)
        );
      },
      {},
      { timeout: 1000 },
    );
    expect(note).toBeVisible();
  });
});
