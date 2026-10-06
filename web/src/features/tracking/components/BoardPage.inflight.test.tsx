import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { USER_A, callsTo, signInAs, stubAccountFetch } from '@/test/accountFetch';

import {
  APPLICATIONS_PATH,
  BOARD_PATH,
  apiError,
  boardServer,
  cardWithText,
  deferred,
  makeCard,
  moveControlOf,
  otherCard,
  regionOf,
  renderBoard,
} from '../test/support';

import type { BoardCard } from '../types';
import type { RouteHandler } from '@/test/accountFetch';

/**
 * Slice 3.1 `/verify` r1 — five frontend defects the first suite let through:
 * focus stolen by a settling move, focus lost on a refused one, a write on card A disturbing card
 * B's in-flight move, the Edit-title button live during its own card's move, and AC-35's missing
 * drop indication. Every held request is a deferred `PUT`, so "while it is in flight" is observed.
 */

const STAGE_PATH = `PUT ${APPLICATIONS_PATH}/:id/stage`;
const A = 'Platform role at Acme';
const B = 'Card number 2';

function serveGated(cards: readonly BoardCard[], overrides: Record<string, RouteHandler> = {}) {
  const gate = deferred<null>();
  const server = boardServer({ 'user-a': cards });
  const fetch = stubAccountFetch({
    ...server.routes(),
    [STAGE_PATH]: async (call, n) => {
      await gate.promise;
      return server.move(call, n);
    },
    ...overrides,
  });
  return {
    server,
    fetch,
    release: () => {
      gate.resolve(null);
    },
  };
}

const boardReads = (fetch: Parameters<typeof callsTo>[0]) =>
  callsTo(fetch, 'GET', BOARD_PATH).length;

async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('Focus after a move settles', () => {
  it('does not take focus from where the user put it while the move was in flight', async () => {
    const { release } = serveGated([makeCard(), otherCard(2)]);
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(A);

    await user.selectOptions(moveControlOf(cardWithText(A)), 'Interviewing');
    await waitFor(() => {
      expect(within(regionOf('Interviewing')).getByText(A)).toBeInTheDocument();
    });
    await user.click(within(cardWithText(B)).getByRole('button', { name: /edit title/i }));
    const input = screen.getByRole('textbox', { name: 'Title' });
    expect(input).toHaveFocus();

    release();
    await waitFor(() => {
      expect(screen.queryByText('Saving…')).not.toBeInTheDocument();
    });
    await settle();

    expect(input).toHaveFocus();
  });

  it("positive: with focus left on the page body, it lands on the moved card's Move control", async () => {
    const { release } = serveGated([makeCard(), otherCard(2)]);
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(A);

    await user.selectOptions(moveControlOf(cardWithText(A)), 'Interviewing');
    await waitFor(() => {
      expect(within(regionOf('Interviewing')).getByText(A)).toBeInTheDocument();
    });
    act(() => {
      (document.activeElement as HTMLElement | null)?.blur();
    });
    expect(document.body).toHaveFocus();

    release();

    await waitFor(() => {
      expect(moveControlOf(cardWithText(A))).toHaveFocus();
    });
  });

  it("a refused move (409) returns focus to that card's Move control", async () => {
    const gate = deferred<null>();
    const server = boardServer({ 'user-a': [makeCard()] });
    stubAccountFetch({
      ...server.routes(),
      [STAGE_PATH]: async () => {
        await gate.promise;
        return apiError(409, 'tracked_application_version_conflict', { current_version: 9 });
      },
    });
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(A);

    await user.selectOptions(moveControlOf(cardWithText(A)), 'Interviewing');
    // Held: the card has really left its column, so the control the user used is gone.
    await waitFor(() => {
      expect(within(regionOf('Interviewing')).getByText(A)).toBeInTheDocument();
    });
    gate.resolve(null);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    await waitFor(() => {
      expect(within(regionOf('To apply')).getByText(A)).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(moveControlOf(cardWithText(A))).toHaveFocus();
    });
  });
});

describe("A write on card A does not disturb card B's move in flight", () => {
  it('removing card A issues no board read until B settles, then exactly one; B never flickers back', async () => {
    const { fetch, release } = serveGated([makeCard(), otherCard(2)]);
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(A);
    await user.selectOptions(moveControlOf(cardWithText(B)), 'Offer');
    await waitFor(() => {
      expect(within(regionOf('Offer')).getByText(B)).toBeInTheDocument();
    });
    const before = boardReads(fetch);

    await user.click(within(cardWithText(A)).getByRole('button', { name: /remove from board/i }));
    await waitFor(() => {
      expect(screen.queryByText(A)).not.toBeInTheDocument();
    });
    await settle();

    expect(boardReads(fetch)).toBe(before);
    expect(within(regionOf('Offer')).getByText(B)).toBeInTheDocument();

    release();
    await waitFor(() => {
      expect(boardReads(fetch)).toBe(before + 1);
    });
    await settle();
    expect(boardReads(fetch)).toBe(before + 1);
    expect(within(regionOf('Offer')).getByText(B)).toBeInTheDocument();
  });

  it('retitling card A issues no board read until B settles, then exactly one; B never flickers back', async () => {
    const { fetch, release } = serveGated([makeCard(), otherCard(2)]);
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(A);
    await user.selectOptions(moveControlOf(cardWithText(B)), 'Offer');
    await waitFor(() => {
      expect(within(regionOf('Offer')).getByText(B)).toBeInTheDocument();
    });
    const before = boardReads(fetch);

    await user.click(within(cardWithText(A)).getByRole('button', { name: /edit title/i }));
    const input = screen.getByRole('textbox', { name: 'Title' });
    await user.clear(input);
    await user.type(input, 'Renamed role{Enter}');
    expect(await screen.findByText('Renamed role')).toBeInTheDocument();
    await settle();

    expect(boardReads(fetch)).toBe(before);
    expect(within(regionOf('Offer')).getByText(B)).toBeInTheDocument();

    release();
    await waitFor(() => {
      expect(boardReads(fetch)).toBe(before + 1);
    });
    await settle();
    expect(boardReads(fetch)).toBe(before + 1);
    expect(within(regionOf('Offer')).getByText(B)).toBeInTheDocument();
  });

  it("while a card's own move is pending its Edit title button is disabled; another card's is not; enabled once settled", async () => {
    const { release } = serveGated([makeCard(), otherCard(2)]);
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(A);

    await user.selectOptions(moveControlOf(cardWithText(A)), 'Interviewing');

    await waitFor(() => {
      expect(within(cardWithText(A)).getByRole('button', { name: /edit title/i })).toBeDisabled();
    });
    expect(within(cardWithText(B)).getByRole('button', { name: /edit title/i })).toBeEnabled();

    release();
    await waitFor(() => {
      expect(within(cardWithText(A)).getByRole('button', { name: /edit title/i })).toBeEnabled();
    });
  });
});

describe('Drop indication (AC-35)', () => {
  function dataTransfer(): DataTransfer {
    const data: Record<string, string> = {};
    return {
      effectAllowed: 'all',
      dropEffect: 'move',
      types: [],
      setData: (type: string, value: string) => {
        data[type] = value;
      },
      getData: (type: string) => data[type] ?? '',
      setDragImage: () => undefined,
    } as unknown as DataTransfer;
  }
  const draggableOf = (title: string): HTMLElement => {
    const found = cardWithText(title).closest<HTMLElement>('[draggable="true"]');
    if (found === null) {
      throw new Error('not draggable');
    }
    return found;
  };
  const isTarget = (label: string): boolean =>
    regionOf(label).getAttribute('data-drop-target') === 'true';

  it('the column under a dragged card carries data-drop-target="true"; the others do not; it clears on leave', async () => {
    serveGated([makeCard()]);
    renderBoard();
    await screen.findByText(A);
    const transfer = dataTransfer();

    fireEvent.dragStart(draggableOf(A), { dataTransfer: transfer });
    fireEvent.dragEnter(regionOf('Interviewing'), { dataTransfer: transfer });
    fireEvent.dragOver(regionOf('Interviewing'), { dataTransfer: transfer });

    expect(isTarget('Interviewing')).toBe(true);
    expect(isTarget('To apply')).toBe(false);
    expect(isTarget('Offer')).toBe(false);

    fireEvent.dragLeave(regionOf('Interviewing'), { dataTransfer: transfer, relatedTarget: null });
    expect(isTarget('Interviewing')).toBe(false);
  });

  it('it clears on drop', async () => {
    serveGated([makeCard()]);
    renderBoard();
    await screen.findByText(A);
    const transfer = dataTransfer();

    fireEvent.dragStart(draggableOf(A), { dataTransfer: transfer });
    fireEvent.dragEnter(regionOf('Offer'), { dataTransfer: transfer });
    expect(isTarget('Offer')).toBe(true);
    fireEvent.drop(regionOf('Offer'), { dataTransfer: transfer });

    expect(isTarget('Offer')).toBe(false);
  });
});
