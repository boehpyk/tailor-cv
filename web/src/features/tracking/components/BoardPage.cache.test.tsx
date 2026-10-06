import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { historyPagesKey } from '@/features/history/hooks/historyKeys';
import { USER_A, signInAs, stubAccountFetch } from '@/test/accountFetch';

import { boardKey } from '../hooks/trackingKeys';
import {
  boardServer,
  cardWithText,
  makeCard,
  moveControlOf,
  newClient,
  otherCard,
  renderBoard,
} from '../test/support';

/**
 * T27 RED — AC-39's cache half: where the board lives, what its mutations are keyed under, and
 * which queries a tracking write invalidates. The client keeps unobserved entries
 * (`gcTime: Infinity`), so "was invalidated" is read from the entry, never from the collector.
 *
 * The sign-out / second-user half runs through the real route table in `boardWiring.test.tsx`.
 */

const TITLE = 'Platform role at Acme';

function serve() {
  const server = boardServer({ 'user-a': [makeCard(), otherCard(2)] });
  stubAccountFetch(server.routes());
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('The board lives under the account (AC-39)', () => {
  it("is cached at ['auth', 'account', userId, 'tracking', 'board']", async () => {
    serve();
    const { queryClient } = renderBoard();
    await screen.findByText(TITLE);

    expect(boardKey('user-a')).toEqual(['auth', 'account', 'user-a', 'tracking', 'board']);
    expect(
      queryClient.getQueryData(['auth', 'account', 'user-a', 'tracking', 'board']),
    ).toBeDefined();
    expect(
      queryClient.getQueryCache().findAll({ queryKey: ['auth', 'account', 'user-a', 'tracking'] }),
    ).toHaveLength(1);
  });

  it("keys every mutation under ['auth', 'account', userId, 'tracking', …]", async () => {
    serve();
    const user = userEvent.setup();
    const { queryClient } = renderBoard();
    await screen.findByText(TITLE);

    await user.selectOptions(moveControlOf(cardWithText(TITLE)), 'Applied');
    await waitFor(() => {
      expect(screen.queryByText('Saving…')).not.toBeInTheDocument();
    });
    await user.click(
      within(cardWithText('Card number 2')).getByRole('button', { name: /remove from board/i }),
    );
    await screen.findByText("Removed from your board. It's still in your history.");

    const keys = queryClient
      .getMutationCache()
      .getAll()
      .map((mutation) => mutation.options.mutationKey);
    expect(keys.length).toBeGreaterThanOrEqual(2);
    for (const key of keys) {
      expect(key?.slice(0, 4)).toEqual(['auth', 'account', 'user-a', 'tracking']);
    }
  });
});

describe("Tracking writes invalidate the history pages' badges (AC-39)", () => {
  async function withHistoryPages() {
    serve();
    const client = newClient();
    client.setQueryData(historyPagesKey('user-a'), { pages: [], pageParams: [] });
    const user = userEvent.setup();
    renderBoard('user-a', client);
    await screen.findByText(TITLE);
    expect(client.getQueryState(historyPagesKey('user-a'))?.isInvalidated).toBe(false);
    return { client, user };
  }

  it('a move does', async () => {
    const { client, user } = await withHistoryPages();

    await user.selectOptions(moveControlOf(cardWithText(TITLE)), 'Applied');

    await waitFor(() => {
      expect(client.getQueryState(historyPagesKey('user-a'))?.isInvalidated).toBe(true);
    });
  });

  it('a retitle does', async () => {
    const { client, user } = await withHistoryPages();
    await user.click(within(cardWithText(TITLE)).getByRole('button', { name: /edit title/i }));

    await user.type(screen.getByRole('textbox', { name: 'Title' }), ' x{Enter}');

    await waitFor(() => {
      expect(client.getQueryState(historyPagesKey('user-a'))?.isInvalidated).toBe(true);
    });
  });

  it('an untrack does', async () => {
    const { client, user } = await withHistoryPages();

    await user.click(
      within(cardWithText(TITLE)).getByRole('button', { name: /remove from board/i }),
    );

    await waitFor(() => {
      expect(client.getQueryState(historyPagesKey('user-a'))?.isInvalidated).toBe(true);
    });
  });
});
