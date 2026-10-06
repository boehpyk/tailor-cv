import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { WorkspaceScopeProvider } from '@/features/scope/WorkspaceScope';
import { USER_A, bearerFor, callsTo, signInAs, stubAccountFetch } from '@/test/accountFetch';

import { TrackButton } from './TrackButton';
import {
  APPLICATIONS_PATH,
  BOARD_PATH,
  apiError,
  boardServer,
  deferred,
  makeCard,
  newClient,
} from '../test/support';
import { TRACK_FAILED_NOTE, TRACK_NOT_TRACKABLE_NOTE, TRACK_TOO_MANY_NOTE } from '../trackingCopy';

import type { BoardCard } from '../types';
import type { RouteHandler } from '@/test/accountFetch';

/**
 * T27 RED — AC-38: the Add to board control and the "On your board · {stage}" badge, read from the
 * board query rather than from a history field. Rendered in account scope as the history row and
 * the run page mount it; where it appears (and where it must not) is `boardWiring.test.tsx`.
 *
 * The copy for the three 409/503 refusals is the spec's "distinct copy" — the spec does not quote
 * it, so the constants are imported and their distinctness is asserted by what the page shows.
 */

function renderButtons(runIds: readonly string[], client = newClient()) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <WorkspaceScopeProvider scope={{ kind: 'account', userId: 'user-a' }}>
          {runIds.map((runId) => (
            <div key={runId} data-testid={runId}>
              <TrackButton userId="user-a" runId={runId} />
            </div>
          ))}
        </WorkspaceScopeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function serve(cards: readonly BoardCard[] = [], overrides: Record<string, RouteHandler> = {}) {
  const server = boardServer({ 'user-a': cards });
  const fetch = stubAccountFetch({ ...server.routes(), ...overrides });
  return { server, fetch };
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('TrackButton — Add to board (AC-38)', () => {
  it('a run that is not on the board offers "Add to board"', async () => {
    serve([]);

    renderButtons(['run-2']);

    expect(await screen.findByRole('button', { name: 'Add to board' })).toBeEnabled();
    expect(screen.queryByRole('link', { name: /on your board/i })).not.toBeInTheDocument();
  });

  it('a run already on the board shows "On your board · {stage}" linking to /board, and no button', async () => {
    const { fetch } = serve([makeCard({ tailoring_run_id: 'run-2', stage: 'applied' })]);

    renderButtons(['run-2']);

    const badge = await screen.findByRole('link', { name: 'On your board · Applied' });
    expect(badge).toHaveAttribute('href', '/board');
    expect(screen.queryByRole('button', { name: 'Add to board' })).not.toBeInTheDocument();
    expect(callsTo(fetch, 'POST', APPLICATIONS_PATH)).toHaveLength(0);
  });

  it('every button on a page reads one shared board query', async () => {
    const { fetch } = serve([makeCard({ tailoring_run_id: 'run-1', stage: 'offer' })]);

    renderButtons(['run-1', 'run-2', 'run-3']);

    expect(await screen.findByRole('link', { name: 'On your board · Offer' })).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Add to board' })).toHaveLength(2);
    expect(callsTo(fetch, 'GET', BOARD_PATH)).toHaveLength(1);
    expect(callsTo(fetch, 'GET', BOARD_PATH)[0]?.authorization).toBe(bearerFor(USER_A));
  });

  it('while pending it reads "Adding…"; on 201 it becomes "On your board · To apply"', async () => {
    const gate = deferred<null>();
    const server = boardServer({ 'user-a': [] });
    const fetch = stubAccountFetch({
      ...server.routes(),
      [`POST ${APPLICATIONS_PATH}`]: async (call, n) => {
        await gate.promise;
        return server.track(call, n);
      },
    });
    const user = userEvent.setup();
    renderButtons(['run-2']);

    await user.click(await screen.findByRole('button', { name: 'Add to board' }));

    expect(await screen.findByText('Adding…')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add to board' })).not.toBeInTheDocument();
    gate.resolve(null);
    const badge = await screen.findByRole('link', { name: 'On your board · To apply' });
    expect(badge).toHaveAttribute('href', '/board');
    const [post] = callsTo(fetch, 'POST', APPLICATIONS_PATH);
    expect(post?.body).toEqual({ tailoring_run_id: 'run-2' });
    expect(post?.authorization).toBe(bearerFor(USER_A));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('409 application_already_tracked is a success: the board is refetched and shows where it is', async () => {
    const server = boardServer({
      'user-a': [makeCard({ tailoring_run_id: 'run-2', stage: 'interviewing' })],
    });
    const fetch = stubAccountFetch({
      ...server.routes(),
      // The first read is stale — it was taken before another tab added the card.
      [`GET ${BOARD_PATH}`]: (call, n) =>
        n === 1
          ? new Response(JSON.stringify({ items: [] }), {
              status: 200,
              headers: { 'Content-Type': 'application/json' },
            })
          : server.getBoard(call, n),
    });
    const user = userEvent.setup();
    renderButtons(['run-2']);

    await user.click(await screen.findByRole('button', { name: 'Add to board' }));

    expect(
      await screen.findByRole('link', { name: 'On your board · Interviewing' }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(callsTo(fetch, 'POST', APPLICATIONS_PATH)).toHaveLength(1);
    expect(callsTo(fetch, 'GET', BOARD_PATH).length).toBeGreaterThanOrEqual(2);
  });

  it.each([
    [
      '409 tailoring_run_not_trackable',
      () => apiError(409, 'tailoring_run_not_trackable'),
      TRACK_NOT_TRACKABLE_NOTE,
    ],
    [
      '409 too_many_tracked_applications',
      () => apiError(409, 'too_many_tracked_applications'),
      TRACK_TOO_MANY_NOTE,
    ],
    ['503 service_unavailable', () => apiError(503, 'service_unavailable'), TRACK_FAILED_NOTE],
  ])('%s: its own sentence as an alert, and the button is back', async (_name, response, copy) => {
    serve([], { [`POST ${APPLICATIONS_PATH}`]: response });
    const user = userEvent.setup();
    renderButtons(['run-2']);

    await user.click(await screen.findByRole('button', { name: 'Add to board' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(copy);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Add to board' })).toBeEnabled();
    });
    expect(screen.queryByRole('link', { name: /on your board/i })).not.toBeInTheDocument();
  });
});
