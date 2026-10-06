import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { WorkspaceScopeProvider } from '@/features/scope/WorkspaceScope';
import { USER_A, signInAs, stubAccountFetch } from '@/test/accountFetch';

import { TrackButton } from './TrackButton';
import { APPLICATIONS_PATH, apiError, boardServer, newClient } from '../test/support';
import { TRACK_NOT_TRACKABLE_NOTE } from '../trackingCopy';

import type { RouteHandler } from '@/test/accountFetch';

/**
 * Slice 3.1 `/verify` r1 — the cap is the **server's** number. The client must not hard-code "500":
 * the limit is a setting (`MAX_TRACKED_APPLICATIONS_PER_USER`), so a copy with a number in it is
 * wrong the day the setting changes. The fake plants a cap of 7 in its message; a client that
 * prints its own sentence shows 500.
 */

const SERVER_MESSAGE = 'Your board holds 7 applications — remove some you no longer need.';

function serve(overrides: Record<string, RouteHandler>) {
  const server = boardServer({ 'user-a': [] });
  stubAccountFetch({ ...server.routes(), ...overrides });
}

function renderButton() {
  return render(
    <QueryClientProvider client={newClient()}>
      <MemoryRouter>
        <WorkspaceScopeProvider scope={{ kind: 'account', userId: 'user-a' }}>
          <TrackButton userId="user-a" runId="run-2" />
        </WorkspaceScopeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  signInAs(USER_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
  __resetForTests();
});

describe('Add to board: the cap comes from the server', () => {
  it("409 too_many_tracked_applications shows the server's sentence, not a number the client wrote", async () => {
    serve({
      [`POST ${APPLICATIONS_PATH}`]: () =>
        apiError(409, 'too_many_tracked_applications', { message: SERVER_MESSAGE }),
    });
    const user = userEvent.setup();
    renderButton();

    await user.click(await screen.findByRole('button', { name: 'Add to board' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(SERVER_MESSAGE);
    expect(alert).not.toHaveTextContent('500');
  });

  it('positive: another 409 keeps the client copy, so only the cap row reads the server', async () => {
    serve({
      [`POST ${APPLICATIONS_PATH}`]: () =>
        apiError(409, 'tailoring_run_not_trackable', { message: SERVER_MESSAGE }),
    });
    const user = userEvent.setup();
    renderButton();

    await user.click(await screen.findByRole('button', { name: 'Add to board' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(TRACK_NOT_TRACKABLE_NOTE);
    expect(alert).not.toHaveTextContent(SERVER_MESSAGE);
  });
});
