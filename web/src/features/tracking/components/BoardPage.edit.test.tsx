import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { USER_A, bearerFor, callsTo, signInAs, stubAccountFetch } from '@/test/accountFetch';
import { jsonResponse } from '@/test/fixtures';

import {
  APPLICATIONS_PATH,
  apiError,
  boardServer,
  cardWithText,
  deferred,
  makeCard,
  otherCard,
  regionOf,
  renderBoard,
} from '../test/support';

import type { BoardCard } from '../types';
import type { RouteHandler } from '@/test/accountFetch';

/**
 * T27 RED — AC-36 (retitle) and AC-37 (untrack). Neither is optimistic: the server normalizes a
 * title, and a removal cannot be taken back (2.2's rule). Each "not optimistic" claim is observed
 * with a **deferred** request, and paired with the positive that the request really is pending.
 */

const TITLE = 'Platform role at Acme';
const TITLE_PATH = `PUT ${APPLICATIONS_PATH}/:id/title`;
const DELETE_PATH = `DELETE ${APPLICATIONS_PATH}/:id`;

function serve(
  cards: readonly BoardCard[] = [makeCard()],
  overrides: Record<string, RouteHandler> = {},
) {
  const server = boardServer({ 'user-a': cards });
  const fetch = stubAccountFetch({ ...server.routes(), ...overrides });
  return { server, fetch };
}

async function openEditor(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  await screen.findByText(TITLE);
  await user.click(within(cardWithText(TITLE)).getByRole('button', { name: /edit title/i }));
  return screen.getByRole('textbox', { name: 'Title' });
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('Retitle (AC-36)', () => {
  it('Edit title opens a labelled input holding the title, capped at 120, with the counter visible', async () => {
    serve();
    const user = userEvent.setup();
    renderBoard();

    const input = await openEditor(user);

    expect(input).toHaveValue(TITLE);
    expect(input).toHaveAttribute('maxlength', '120');
    expect(screen.getByText(/\/\s*120/)).toBeInTheDocument();
  });

  it("Enter sends PUT …/title with the card's version; nothing changes on screen until the server answers", async () => {
    const gate = deferred<null>();
    const server = boardServer({ 'user-a': [makeCard()] });
    const fetch = stubAccountFetch({
      ...server.routes(),
      [TITLE_PATH]: async (call, n) => {
        await gate.promise;
        return server.retitle(call, n);
      },
    });
    const user = userEvent.setup();
    renderBoard();
    const input = await openEditor(user);

    await user.clear(input);
    await user.type(input, 'Dream job{Enter}');

    await waitFor(() => {
      expect(callsTo(fetch, 'PUT', `${APPLICATIONS_PATH}/:id/title`)).toHaveLength(1);
    });
    const [put] = callsTo(fetch, 'PUT', `${APPLICATIONS_PATH}/:id/title`);
    expect(put?.path).toBe(`${APPLICATIONS_PATH}/app-1/title`);
    expect(put?.body).toEqual({ title: 'Dream job', version: 3 });
    expect(put?.authorization).toBe(bearerFor(USER_A));
    expect(screen.getByText('Saving…')).toBeInTheDocument();
    expect(screen.queryByText('Dream job', { selector: ':not(input)' })).not.toBeInTheDocument();
    gate.resolve(null);
    expect(await screen.findByText('Dream job')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Title' })).not.toBeInTheDocument();
  });

  it("the Save button sends the same request, and shows the server's normalized title", async () => {
    const { fetch } = serve();
    const user = userEvent.setup();
    renderBoard();
    const input = await openEditor(user);

    await user.clear(input);
    await user.type(input, '  Padded title  ');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText('Padded title')).toBeInTheDocument();
    expect(callsTo(fetch, 'PUT', `${APPLICATIONS_PATH}/:id/title`)).toHaveLength(1);
  });

  it('Clear title sends null, and the card falls back to the posting title', async () => {
    const { fetch } = serve();
    const user = userEvent.setup();
    renderBoard();
    await openEditor(user);

    await user.click(screen.getByRole('button', { name: 'Clear title' }));

    expect(await screen.findByText('Senior Platform Engineer')).toBeInTheDocument();
    expect(callsTo(fetch, 'PUT', `${APPLICATIONS_PATH}/:id/title`)[0]?.body).toEqual({
      title: null,
      version: 3,
    });
  });

  it('Escape cancels without a request and returns focus to Edit title', async () => {
    const { fetch } = serve();
    const user = userEvent.setup();
    renderBoard();
    const input = await openEditor(user);

    await user.type(input, ' extra{Escape}');

    await waitFor(() => {
      expect(screen.queryByRole('textbox', { name: 'Title' })).not.toBeInTheDocument();
    });
    expect(screen.getByText(TITLE)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /edit title/i })).toHaveFocus();
    expect(callsTo(fetch, 'PUT', `${APPLICATIONS_PATH}/:id/title`)).toHaveLength(0);
  });

  it("422: the boundary's message sits under the field, linked by aria-describedby, and the input keeps what was typed", async () => {
    serve([makeCard()], {
      [TITLE_PATH]: () =>
        jsonResponse(422, {
          error: { code: 'validation_error', message: 'Titles cannot contain control characters' },
        }),
    });
    const user = userEvent.setup();
    renderBoard();
    const input = await openEditor(user);

    await user.clear(input);
    await user.type(input, 'Bad title{Enter}');

    const message = await screen.findByText('Titles cannot contain control characters');
    const describedBy =
      screen.getByRole('textbox', { name: 'Title' }).getAttribute('aria-describedby') ?? '';
    expect(describedBy.split(/\s+/)).toContain(message.id);
    expect(message.id).not.toBe('');
    expect(screen.getByRole('textbox', { name: 'Title' })).toHaveValue('Bad title');
  });

  it('409: "changed in another tab", as a rejected move', async () => {
    serve([makeCard()], {
      [TITLE_PATH]: () =>
        apiError(409, 'tracked_application_version_conflict', { current_version: 8 }),
    });
    const user = userEvent.setup();
    renderBoard();
    const input = await openEditor(user);

    await user.type(input, ' x{Enter}');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This application changed in another tab — your board is up to date.',
    );
  });
});

describe('Untrack (AC-37)', () => {
  it('Remove from board shows "Removing…" and does NOT remove the card until the server says 204', async () => {
    const gate = deferred<null>();
    const server = boardServer({ 'user-a': [makeCard(), otherCard(2)] });
    const fetch = stubAccountFetch({
      ...server.routes(),
      [DELETE_PATH]: async (call, n) => {
        await gate.promise;
        return server.untrack(call, n);
      },
    });
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(TITLE);

    await user.click(
      within(cardWithText(TITLE)).getByRole('button', { name: /remove from board/i }),
    );

    await waitFor(() => {
      expect(callsTo(fetch, 'DELETE', `${APPLICATIONS_PATH}/:id`)).toHaveLength(1);
    });
    expect(callsTo(fetch, 'DELETE', `${APPLICATIONS_PATH}/:id`)[0]?.path).toBe(
      `${APPLICATIONS_PATH}/app-1`,
    );
    expect(screen.getByText('Removing…')).toBeInTheDocument();
    expect(within(regionOf('To apply')).getByText(TITLE)).toBeInTheDocument();
    gate.resolve(null);
    await waitFor(() => {
      expect(screen.queryByText(TITLE)).not.toBeInTheDocument();
    });
  });

  it('204: the card is gone and a polite note says it is still in the history', async () => {
    serve([makeCard(), otherCard(2)]);
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(TITLE);

    await user.click(
      within(cardWithText(TITLE)).getByRole('button', { name: /remove from board/i }),
    );

    const note = await screen.findByText("Removed from your board. It's still in your history.");
    expect(note.closest('[aria-live="polite"], [role="status"]')).not.toBeNull();
    expect(screen.queryByText(TITLE)).not.toBeInTheDocument();
    expect(screen.getByText('Card number 2')).toBeInTheDocument();
  });

  it('404: the card is gone too — it was already off the board', async () => {
    const server = boardServer({ 'user-a': [makeCard(), otherCard(2)] });
    stubAccountFetch({
      ...server.routes(),
      [DELETE_PATH]: (call, n) => {
        void server.untrack(call, n);
        return apiError(404, 'tracked_application_not_found');
      },
    });
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(TITLE);

    await user.click(
      within(cardWithText(TITLE)).getByRole('button', { name: /remove from board/i }),
    );

    await waitFor(() => {
      expect(screen.queryByText(TITLE)).not.toBeInTheDocument();
    });
    expect(screen.getByText('Card number 2')).toBeInTheDocument();
  });

  it('503: the card is kept, with "Not removed — try again."', async () => {
    serve([makeCard(), otherCard(2)], {
      [DELETE_PATH]: () => apiError(503, 'service_unavailable'),
    });
    const user = userEvent.setup();
    renderBoard();
    await screen.findByText(TITLE);

    await user.click(
      within(cardWithText(TITLE)).getByRole('button', { name: /remove from board/i }),
    );

    expect(await screen.findByText('Not removed — try again.')).toBeInTheDocument();
    expect(within(regionOf('To apply')).getByText(TITLE)).toBeInTheDocument();
    expect(screen.queryByText('Removing…')).not.toBeInTheDocument();
    expect(
      within(cardWithText(TITLE)).getByRole('button', { name: /remove from board/i }),
    ).toBeEnabled();
  });
});
