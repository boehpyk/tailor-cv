import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import {
  USER_A,
  bearerFor,
  callsTo,
  hang,
  ok,
  signInAs,
  stubAccountFetch,
  tokenFor,
} from '@/test/accountFetch';

import { boardKey } from '../hooks/trackingKeys';
import {
  APPLICATIONS_PATH,
  BOARD_PATH,
  apiError,
  boardServer,
  cardWithText,
  deferred,
  makeCard,
  moveControlOf,
  newClient,
  otherCard,
  regionOf,
  renderBoard,
} from '../test/support';

import type { BoardCard } from '../types';
import type { RouteHandler } from '@/test/accountFetch';

/**
 * T27 RED — AC-33 (move by control), AC-34 (refusals roll back, each with its own copy), AC-35
 * (move by drag) and T-39 / T-41 / T-42.
 *
 * Every optimistic claim is made with a **deferred** `PUT`: the request is held open, so "the card
 * moved before the server answered" is observed rather than inferred from a fast stub. The skeleton
 * renders placeholders and its mutation rejects, so every test here is red on the behaviour.
 */

const STAGE_PATH = `PUT ${APPLICATIONS_PATH}/:id/stage`;
const TITLE = 'Platform role at Acme';

function serve(
  cards: readonly BoardCard[] = [makeCard()],
  overrides: Record<string, RouteHandler> = {},
) {
  const server = boardServer({ 'user-a': cards });
  const fetch = stubAccountFetch({ ...server.routes(), ...overrides });
  return { server, fetch };
}

/** A server whose `PUT …/stage` waits for `release()` and then behaves like the real one. */
function serveGated(cards: readonly BoardCard[] = [makeCard()]) {
  const gate = deferred<null>();
  const server = boardServer({ 'user-a': cards });
  const fetch = stubAccountFetch({
    ...server.routes(),
    [STAGE_PATH]: async (call, n) => {
      await gate.promise;
      return server.move(call, n);
    },
  });
  return {
    server,
    fetch,
    release: () => {
      gate.resolve(null);
    },
  };
}

function stagePuts(fetch: { calls: readonly { method: string; path: string }[] }) {
  return fetch.calls.filter((call) => call.method === 'PUT' && call.path.endsWith('/stage'));
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('Move by control (AC-33, T-39)', () => {
  it("moves the card at once — before the request resolves — and sends the card's version", async () => {
    const { fetch, release } = serveGated();
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(TITLE);

    await user.selectOptions(moveControlOf(cardWithText(TITLE)), 'Interviewing');

    await waitFor(() => {
      expect(within(regionOf('Interviewing')).getByText(TITLE)).toBeInTheDocument();
    });
    expect(within(regionOf('To apply')).queryByText(TITLE)).not.toBeInTheDocument();
    expect(regionOf('Interviewing')).toHaveAccessibleName(/\b1\b/);
    expect(regionOf('To apply')).toHaveAccessibleName(/\b0\b/);
    const [put] = callsTo(fetch, 'PUT', `${APPLICATIONS_PATH}/:id/stage`);
    expect(put?.path).toBe(`${APPLICATIONS_PATH}/app-1/stage`);
    expect(put?.body).toEqual({ stage: 'interviewing', version: 3 });
    expect(put?.authorization).toBe(bearerFor(USER_A));
    release();
    await waitFor(() => {
      expect(screen.queryByText('Saving…')).not.toBeInTheDocument();
    });
    expect(within(regionOf('Interviewing')).getByText(TITLE)).toBeInTheDocument();
  });

  it('while the move is pending, that card\'s control is disabled and says "Saving…"; another card is not blocked', async () => {
    const { release } = serveGated([makeCard(), otherCard(2)]);
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(TITLE);

    await user.selectOptions(moveControlOf(cardWithText(TITLE)), 'Offer');

    const moved = await within(regionOf('Offer')).findByText(TITLE);
    expect(moved).toBeInTheDocument();
    expect(screen.getByText('Saving…')).toBeInTheDocument();
    expect(moveControlOf(cardWithText(TITLE))).toBeDisabled();
    expect(moveControlOf(cardWithText('Card number 2'))).toBeEnabled();
    release();
    await waitFor(() => {
      expect(moveControlOf(cardWithText(TITLE))).toBeEnabled();
    });
  });

  it("puts keyboard focus on the moved card's Move control in its new column", async () => {
    const { release } = serveGated();
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(TITLE);

    await user.selectOptions(moveControlOf(cardWithText(TITLE)), 'Interviewing');
    release();

    await waitFor(() => {
      expect(
        within(regionOf('Interviewing')).getByRole('combobox', { name: /move to/i }),
      ).toHaveFocus();
    });
  });

  it('announces "Moved {title} to {stage}." in a polite live region', async () => {
    const { release } = serveGated();
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(TITLE);

    await user.selectOptions(moveControlOf(cardWithText(TITLE)), 'Interviewing');
    release();

    const announcement = await screen.findByText(`Moved ${TITLE} to Interviewing.`);
    expect(announcement.closest('[aria-live="polite"], [role="status"]')).not.toBeNull();
    expect(announcement.closest('[aria-live="assertive"], [role="alert"]')).toBeNull();
  });

  it('a same-tick double choice sends one request — the isMutating guard, not isPending', async () => {
    const { fetch, release } = serveGated();
    renderBoard();
    await screen.findByText(TITLE);
    const control = moveControlOf(cardWithText(TITLE));

    act(() => {
      fireEvent.change(control, { target: { value: 'interviewing' } });
      fireEvent.change(control, { target: { value: 'offer' } });
    });
    await waitFor(() => {
      expect(stagePuts(fetch)).toHaveLength(1);
    });
    release();
    await waitFor(() => {
      expect(screen.queryByText('Saving…')).not.toBeInTheDocument();
    });

    expect(stagePuts(fetch)).toHaveLength(1);
    expect(callsTo(fetch, 'PUT', `${APPLICATIONS_PATH}/:id/stage`)[0]?.body).toEqual({
      stage: 'interviewing',
      version: 3,
    });
  });

  it("replaces the card with the server's: a second move sends the new version", async () => {
    const { fetch } = serve();
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(TITLE);

    await user.selectOptions(moveControlOf(cardWithText(TITLE)), 'Applied');
    await waitFor(() => {
      expect(screen.queryByText('Saving…')).not.toBeInTheDocument();
    });
    await user.selectOptions(moveControlOf(cardWithText(TITLE)), 'Offer');

    await waitFor(() => {
      expect(stagePuts(fetch)).toHaveLength(2);
    });
    expect(callsTo(fetch, 'PUT', `${APPLICATIONS_PATH}/:id/stage`)[1]?.body).toEqual({
      stage: 'offer',
      version: 4,
    });
  });
});

describe('A refetch landing mid-move cannot undo it (T-41)', () => {
  it('the in-flight board read is cancelled, so its stale answer never overwrites the optimistic state', async () => {
    const server = boardServer({ 'user-a': [makeCard()] });
    const stale = deferred<null>();
    const gate = deferred<null>();
    const fetch = stubAccountFetch({
      ...server.routes(),
      [`GET ${BOARD_PATH}`]: async (call, n) => {
        if (n === 2) {
          await stale.promise;
        }
        return server.getBoard(call, n);
      },
      [STAGE_PATH]: async (call, n) => {
        await gate.promise;
        return server.move(call, n);
      },
    });
    const client = newClient();
    const user = userEvent.setup();
    renderBoard('user-a', client);
    await screen.findByText(TITLE);

    act(() => {
      void client.invalidateQueries({ queryKey: boardKey('user-a') });
    });
    await waitFor(() => {
      expect(callsTo(fetch, 'GET', BOARD_PATH)).toHaveLength(2);
    });
    await user.selectOptions(moveControlOf(cardWithText(TITLE)), 'Interviewing');
    await within(regionOf('Interviewing')).findByText(TITLE);
    await act(async () => {
      stale.resolve(null);
      await new Promise((resolve) => setTimeout(resolve, 30));
    });

    expect(within(regionOf('Interviewing')).getByText(TITLE)).toBeInTheDocument();
    expect(within(regionOf('To apply')).queryByText(TITLE)).not.toBeInTheDocument();
    gate.resolve(null);
  });
});

describe('A refused move rolls back with its own copy (AC-34)', () => {
  /** A refusal whose refetch hangs: only a restored snapshot can bring the card back. */
  function refuse(response: () => Response | Promise<Response>) {
    const server = boardServer({ 'user-a': [makeCard()] });
    stubAccountFetch({
      ...server.routes(),
      [`GET ${BOARD_PATH}`]: (call, n) => (n === 1 ? server.getBoard(call, n) : hang()),
      [STAGE_PATH]: response,
    });
  }

  it.each([
    [
      '429 rate_limited',
      () => apiError(429, 'rate_limited', {}, { 'Retry-After': '30' }),
      // AC-16 supersedes "wait a moment": the 30 s Retry-After is named.
      /Too many changes.*try again in 30 seconds/i,
    ],
    [
      '503 service_unavailable',
      () => apiError(503, 'service_unavailable'),
      'Not moved — try again.',
    ],
    [
      'a network failure',
      () => Promise.reject(new TypeError('Failed to fetch')),
      'Not moved — try again.',
    ],
  ])(
    '%s: the card goes back from the snapshot (the refetch hangs) and the reason is an alert',
    async (_name, response, copy) => {
      refuse(response);
      const user = userEvent.setup();
      renderBoard();
      await screen.findByText(TITLE);

      await user.selectOptions(moveControlOf(cardWithText(TITLE)), 'Interviewing');

      expect(await screen.findByRole('alert')).toHaveTextContent(copy);
      await waitFor(() => {
        expect(within(regionOf('To apply')).getByText(TITLE)).toBeInTheDocument();
      });
      expect(within(regionOf('Interviewing')).queryByText(TITLE)).not.toBeInTheDocument();
    },
  );

  it('409 version conflict: restored, the board refetched, and "changed in another tab"', async () => {
    const { fetch } = serve([makeCard()], {
      [STAGE_PATH]: () =>
        apiError(409, 'tracked_application_version_conflict', { current_version: 9 }),
    });
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(TITLE);

    await user.selectOptions(moveControlOf(cardWithText(TITLE)), 'Interviewing');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This application changed in another tab — your board is up to date.',
    );
    await waitFor(() => {
      expect(callsTo(fetch, 'GET', BOARD_PATH).length).toBeGreaterThanOrEqual(2);
    });
    expect(within(regionOf('To apply')).getByText(TITLE)).toBeInTheDocument();
  });

  it('404: the card is removed, with "no longer on your board"', async () => {
    const server = boardServer({ 'user-a': [makeCard(), otherCard(2)] });
    stubAccountFetch({
      ...server.routes(),
      // The refetch hangs (as in the cases above): only `onError` itself can remove the card.
      // Mutation-checked: restoring the snapshot instead of `withoutCard` leaves it on the board.
      [`GET ${BOARD_PATH}`]: (call, n) => (n === 1 ? server.getBoard(call, n) : hang()),
      [STAGE_PATH]: () => {
        server.cardsOf('user-a').splice(0, 1);
        return apiError(404, 'tracked_application_not_found');
      },
    });
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(TITLE);

    await user.selectOptions(moveControlOf(cardWithText(TITLE)), 'Interviewing');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This application is no longer on your board.',
    );
    await waitFor(() => {
      expect(screen.queryByText(TITLE)).not.toBeInTheDocument();
    });
    expect(screen.getByText('Card number 2')).toBeInTheDocument();
  });
});

describe('Overlapping moves: one refusal never disturbs the other card (AC-34, T-39)', () => {
  const OTHER = 'Card number 2';

  /**
   * Two cards, each move held on its **own** gate, so the order of settling is the test's. `refuseA`
   * answers card A's `PUT`; card B's is answered by the stateful fake once released.
   *
   * Guards (slice 3.1 `/verify` round 1, MAJOR) — each mutation-checked against
   * `hooks/useMoveTrackedApplication.ts`, restored byte-exact:
   * - M1: `onError` restores the whole `context.previous` instead of only the refused card. Red
   *   (both cases, 2 failed | 16 passed): "Unable to find an element with the text: Card number 2"
   *   in the Offer column — B was put back while its own request was still pending.
   * - M2: `onSettled` invalidates unconditionally (`if (true)` for the `=== 1` guard). Red (both
   *   cases, 2 failed | 16 passed): the same "Card number 2" error — A's refusal refetched, and a
   *   server that had not seen B's move overwrote B's optimistic state.
   * - M3 (the 404 case below): `onError` restores the card instead of `withoutCard`. Red (1 failed |
   *   17 passed): "expected document not to contain element" — the card stays on the board.
   */
  function serveOverlap(refuseA: () => Response) {
    const gateA = deferred<null>();
    const gateB = deferred<null>();
    const server = boardServer({ 'user-a': [makeCard(), otherCard(2)] });
    const fetch = stubAccountFetch({
      ...server.routes(),
      [STAGE_PATH]: async (call, n) => {
        if (call.path === `${APPLICATIONS_PATH}/app-1/stage`) {
          await gateA.promise;
          return refuseA();
        }
        await gateB.promise;
        return server.move(call, n);
      },
    });
    return {
      fetch,
      releaseA: () => {
        gateA.resolve(null);
      },
      releaseB: () => {
        gateB.resolve(null);
      },
    };
  }

  const settle = () =>
    act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 40));
    });

  it.each([
    [
      '409 version conflict',
      () => apiError(409, 'tracked_application_version_conflict', { current_version: 9 }),
      'This application changed in another tab — your board is up to date.',
    ],
    [
      '503 service_unavailable',
      () => apiError(503, 'service_unavailable'),
      'Not moved — try again.',
    ],
  ])(
    "%s on card A while card B's move is pending: A goes back with its message, B stays moved, and no board GET goes out until B settles",
    async (_name, refuseA, copy) => {
      const { fetch, releaseA, releaseB } = serveOverlap(refuseA);
      const user = userEvent.setup();
      renderBoard();
      await screen.findByText(TITLE);
      expect(callsTo(fetch, 'GET', BOARD_PATH)).toHaveLength(1);

      await user.selectOptions(moveControlOf(cardWithText(TITLE)), 'Interviewing');
      await user.selectOptions(moveControlOf(cardWithText(OTHER)), 'Offer');
      await within(regionOf('Offer')).findByText(OTHER);
      await within(regionOf('Interviewing')).findByText(TITLE);

      releaseA();

      expect(await screen.findByRole('alert')).toHaveTextContent(copy);
      await waitFor(() => {
        expect(within(regionOf('To apply')).getByText(TITLE)).toBeInTheDocument();
      });
      expect(within(regionOf('Interviewing')).queryByText(TITLE)).not.toBeInTheDocument();
      expect(within(regionOf('Offer')).getByText(OTHER)).toBeInTheDocument();
      expect(within(regionOf('To apply')).queryByText(OTHER)).not.toBeInTheDocument();
      await settle();
      expect(callsTo(fetch, 'GET', BOARD_PATH)).toHaveLength(1);
      expect(within(regionOf('Offer')).getByText(OTHER)).toBeInTheDocument();

      releaseB();

      await waitFor(() => {
        expect(callsTo(fetch, 'GET', BOARD_PATH)).toHaveLength(2);
      });
      await settle();
      expect(callsTo(fetch, 'GET', BOARD_PATH)).toHaveLength(2);
      expect(within(regionOf('Offer')).getByText(OTHER)).toBeInTheDocument();
      expect(within(regionOf('To apply')).getByText(TITLE)).toBeInTheDocument();
    },
  );
});

describe('A 401 mid-move is refreshed once and retried (T-42)', () => {
  it('the optimistic move survives the refresh and the retry lands', async () => {
    const server = boardServer({ 'user-a': [makeCard()] });
    let attempt = 0;
    const fetch = stubAccountFetch({
      ...server.routes(),
      [STAGE_PATH]: (call, n) => {
        attempt += 1;
        return attempt === 1 ? apiError(401, 'invalid_access_token') : server.move(call, n);
      },
      'POST /api/auth/refresh': ok({
        access_token: `${tokenFor(USER_A)}-refreshed`,
        token_type: 'Bearer',
        expires_in: 900,
        user: USER_A,
      }),
    });
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(TITLE);

    await user.selectOptions(moveControlOf(cardWithText(TITLE)), 'Interviewing');

    await waitFor(() => {
      expect(stagePuts(fetch)).toHaveLength(2);
    });
    expect(callsTo(fetch, 'POST', '/api/auth/refresh')).toHaveLength(1);
    expect(within(regionOf('Interviewing')).getByText(TITLE)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

// --- AC-35: drag --------------------------------------------------------------------------------------

function dataTransfer(): DataTransfer {
  const data: Record<string, string> = {};
  return {
    data,
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

function draggableOf(title: string): HTMLElement {
  const draggable = cardWithText(title).closest<HTMLElement>('[draggable="true"]');
  if (draggable === null) {
    throw new Error(`the card ${JSON.stringify(title)} is not draggable`);
  }
  return draggable;
}

function dragCardTo(title: string, column: HTMLElement): void {
  const transfer = dataTransfer();
  fireEvent.dragStart(draggableOf(title), { dataTransfer: transfer });
  fireEvent.dragEnter(column, { dataTransfer: transfer });
  fireEvent.dragOver(column, { dataTransfer: transfer });
  fireEvent.drop(column, { dataTransfer: transfer });
  fireEvent.dragEnd(draggableOf(title), { dataTransfer: transfer });
}

describe('Move by drag (AC-35, T-40)', () => {
  it('dropping a card on another column issues the same request, and the same optimistic state, as the control', async () => {
    const { fetch, release } = serveGated();
    renderBoard();
    await screen.findByText(TITLE);

    dragCardTo(TITLE, regionOf('Offer'));

    await waitFor(() => {
      expect(within(regionOf('Offer')).getByText(TITLE)).toBeInTheDocument();
    });
    expect(within(regionOf('To apply')).queryByText(TITLE)).not.toBeInTheDocument();
    const [put] = callsTo(fetch, 'PUT', `${APPLICATIONS_PATH}/:id/stage`);
    expect(put?.path).toBe(`${APPLICATIONS_PATH}/app-1/stage`);
    expect(put?.body).toEqual({ stage: 'offer', version: 3 });
    release();
  });

  it('dropping on its own column sends nothing — while a drop elsewhere sends exactly one', async () => {
    const { fetch, release } = serveGated();
    renderBoard();
    await screen.findByText(TITLE);

    dragCardTo(TITLE, regionOf('To apply'));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30));
    });
    expect(stagePuts(fetch)).toHaveLength(0);

    dragCardTo(TITLE, regionOf('Rejected'));
    await waitFor(() => {
      expect(stagePuts(fetch)).toHaveLength(1);
    });
    release();
  });

  it('every card is draggable, and the Move control stays available beside it (T-40)', async () => {
    serve([makeCard(), otherCard(2, { stage: 'offer' })]);
    renderBoard();
    await screen.findByText(TITLE);

    expect(draggableOf(TITLE)).toHaveAttribute('draggable', 'true');
    expect(draggableOf('Card number 2')).toHaveAttribute('draggable', 'true');
    expect(moveControlOf(cardWithText(TITLE))).toBeVisible();
  });
});
