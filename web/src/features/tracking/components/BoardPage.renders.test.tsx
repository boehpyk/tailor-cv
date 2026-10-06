import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { USER_A, signInAs, stubAccountFetch } from '@/test/accountFetch';

import {
  APPLICATIONS_PATH,
  boardServer,
  cardWithText,
  deferred,
  moveControlOf,
  otherCard,
  renderBoard,
} from '../test/support';

import type { BoardCard } from '../types';
import type * as CardTitleEditorModule from './CardTitleEditor';

/**
 * AC-44 guard: an optimistic move re-renders the moved card and nothing else, however big the board.
 *
 * **How the render is counted without touching production code.** `BoardCard` renders exactly one
 * `CardTitleEditor` per render, passing its card id as `applicationId`. `vi.mock` swaps that module
 * for a thin wrapper that bumps a per-id counter and then renders the real editor. The wrapper is
 * not memoized, so it renders once per render of its parent, `BoardCard`: its count is that card's
 * render count. (A proxy, honestly: it counts the card's render, not React's internals. A memoized
 * `BoardCard` that bails out never reaches the wrapper; one that re-renders always does.)
 *
 * The `PUT` is held, so the window counted is purely the optimistic edit — not the settle refetch.
 *
 * Mutations, each observed red (3.1 AC-44 guard; numbers are the renders of non-moved cards after one
 * move, at 20 and 60 cards; healthy is 0 / 0):
 *  - remove `structuralSharing: shareCardsById` from `boardQueryOptions`: 19 / 59 — every card whose
 *    index shifted comes back as a fresh copy.
 *  - replace `shareCardsById`'s body with `replaceEqualDeep(previous, next)`: 19 / 59, the same.
 *  - pass an inline arrow as `onMove` in `BoardPage`: 38 / 118 — every card gets a new prop on each
 *    page render (two per move).
 *  Each fails "no other card re-rendered" at both sizes, and the 60-card test shows the count scaling
 *  with the board.
 */

const renders = new Map<string, number>();

vi.mock('./CardTitleEditor', async (importOriginal) => {
  const actual = await importOriginal<typeof CardTitleEditorModule>();
  return {
    ...actual,
    CardTitleEditor: (props: Parameters<typeof actual.CardTitleEditor>[0]) => {
      renders.set(props.applicationId, (renders.get(props.applicationId) ?? 0) + 1);
      return <actual.CardTitleEditor {...props} />;
    },
  };
});

const STAGE_PATH = `PUT ${APPLICATIONS_PATH}/:id/stage`;

/**
 * Cards in descending `stage_changed_at`, so the board's order is 1..n. The moved card is the
 * **last** one: the move gives it the newest time and sorts it to the front, shifting every other
 * card's index by one — the case positional sharing gets wrong.
 */
function cards(n: number): BoardCard[] {
  return Array.from({ length: n }, (_, i) =>
    otherCard(i + 1, {
      stage_changed_at: new Date(Date.UTC(2026, 8, 20, 0, 0, 0) - i * 60_000)
        .toISOString()
        .replace('.000Z', 'Z'),
    }),
  );
}

async function moveLastCard(n: number): Promise<{ moved: number; others: number }> {
  const gate = deferred<null>();
  const server = boardServer({ 'user-a': cards(n) });
  stubAccountFetch({
    ...server.routes(),
    [STAGE_PATH]: async (call, count) => {
      await gate.promise;
      return server.move(call, count);
    },
  });
  const user = userEvent.setup();
  renderBoard();
  const lastTitle = `Card number ${String(n)}`;
  await screen.findByText(lastTitle);
  await screen.findByText('Card number 1');

  renders.clear();
  await user.selectOptions(moveControlOf(cardWithText(lastTitle)), 'Interviewing');
  await waitFor(() => {
    expect(screen.getByText('Saving…')).toBeInTheDocument();
  });
  const movedId = `app-${String(n)}`;
  const moved = renders.get(movedId) ?? 0;
  const others = [...renders].filter(([id]) => id !== movedId).reduce((sum, [, c]) => sum + c, 0);
  gate.resolve(null);
  await waitFor(() => {
    expect(screen.queryByText('Saving…')).not.toBeInTheDocument();
  });
  return { moved, others };
}

beforeEach(() => {
  signInAs(USER_A);
  renders.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('An optimistic move re-renders only the moved card (AC-44)', () => {
  it('renders the moved card a small constant number of times and no other card, at 20 cards', async () => {
    const { moved, others } = await moveLastCard(20);

    expect(moved).toBeGreaterThanOrEqual(1);
    expect(moved).toBeLessThanOrEqual(3);
    expect(others).toBe(0);
  });

  it('does not scale with the board: the same counts at 60 cards', async () => {
    const { moved, others } = await moveLastCard(60);

    expect(moved).toBeGreaterThanOrEqual(1);
    expect(moved).toBeLessThanOrEqual(3);
    expect(others).toBe(0);
  });
});
