import { QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { authStore, __resetForTests } from '@/features/auth/authStore';
import { WorkspaceScopeProvider } from '@/features/scope/WorkspaceScope';
import { createAppQueryClient } from '@/queryClient';
import {
  USER_A,
  bearerFor,
  hang,
  signInAs,
  status,
  stubAccountFetch,
  type RouteHandler,
} from '@/test/accountFetch';
import { jsonResponse } from '@/test/fixtures';

import {
  ACCOUNT_BUSY,
  ACCOUNT_RETENTION,
  CHECK_BUTTON,
  ESTIMATE_LABEL,
  FAILED,
  FIT_SCORE_NOTICE,
  GAPS_HEADING,
  GUEST_BUSY,
  GUEST_LIMIT,
  GUEST_RETENTION,
  LOAD_ERROR,
  NOT_READY,
  NO_CLEAR_GAP,
  REFUSED,
  RETRY_LOAD,
  SESSION_EXPIRED,
  SIGN_UP_LINK,
  TRY_AGAIN,
  WORKING,
} from '../fitScoreCopy';
import { FitScorePanel } from './FitScorePanel';

import type { FitScore, FitScorePair } from '../types';
import type { WorkspaceScope } from '@/features/scope/scopeMap';

/**
 * T38 RED (slice 5.1; AC-15, AC-45 to AC-48, technical plan §6 and its "Precedence corrected
 * 2026-11-01"): the panel through a fake server whose bodies have the real response shapes
 * (`schemas/scoring.py`: `{fit_score: FitScore | null}` on GET, a `FitScore` on POST, the error
 * envelope with `retry_not_before` beside `code`), in both scopes, on the app's own client.
 *
 * Traps honoured: a disabled button satisfies `findByRole`, so "enabled" is asserted with
 * `toBeEnabled()`; every absence has a positive control; a `POST` is counted on the wire, not
 * inferred from the UI; the live region is asserted by role, after it fills.
 */

const PAIR: FitScorePair = {
  base_cv_id: '0192f0a1-aaaa-7000-8000-00000000aaaa',
  job_posting_id: '0192f0a1-bbbb-7000-8000-00000000bbbb',
};

const SCORE: FitScore = {
  id: '0190a000-0000-7000-8000-000000000001',
  score_percent: 62,
  gaps: ['Kubernetes', 'Terraform'],
  scored_at: '2026-10-31T10:00:00Z',
  expires_at: null,
};

const GUEST: WorkspaceScope = { kind: 'guest' };
const ACCOUNT: WorkspaceScope = { kind: 'account', userId: USER_A.id };

interface World {
  /** What the server has stored for the pair. Handlers read it, so a test can change it mid-flight. */
  stored: FitScore | null;
}

/** GET answers from `world`; POST is the test's own. */
function routes(world: World, post: RouteHandler): Record<string, RouteHandler> {
  const get: RouteHandler = () => jsonResponse(200, { fit_score: world.stored });
  return {
    'GET /api/fit-scores': get,
    'GET /api/me/fit-scores': get,
    'POST /api/fit-scores': post,
    'POST /api/me/fit-scores': post,
  };
}

function renderPanel(scope: WorkspaceScope, pair: FitScorePair | null = PAIR): void {
  if (scope.kind === 'account') {
    signInAs(USER_A);
  }
  render(
    <QueryClientProvider client={createAppQueryClient()}>
      <MemoryRouter>
        <WorkspaceScopeProvider scope={scope}>
          <FitScorePanel pair={pair} />
        </WorkspaceScopeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const checkButton = (): HTMLElement => screen.getByRole('button', { name: CHECK_BUTTON });
const posts = (f: ReturnType<typeof stubAccountFetch>) =>
  f.calls.filter((c) => c.method === 'POST' && c.path.endsWith('/fit-scores'));
const gets = (f: ReturnType<typeof stubAccountFetch>) => f.calls.filter((c) => c.method === 'GET');

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  __resetForTests();
});

describe.each([
  { name: 'guest', scope: GUEST, path: '/api/fit-scores', retention: GUEST_RETENTION },
  { name: 'account', scope: ACCOUNT, path: '/api/me/fit-scores', retention: ACCOUNT_RETENTION },
])('FitScorePanel ($name)', ({ scope, path, retention }) => {
  const isAccount = scope.kind === 'account';

  it('reads the pair from its scope path with its scope credential, and posts nothing on render (AC-48)', async () => {
    const f = stubAccountFetch(routes({ stored: null }, status(500, 'boom')));
    renderPanel(scope);
    await screen.findByRole('button', { name: CHECK_BUTTON });

    expect(f.calls).toHaveLength(1);
    const [read] = f.calls;
    expect(read?.method).toBe('GET');
    expect(read?.path).toBe(path);
    expect(read?.query.get('base_cv_id')).toBe(PAIR.base_cv_id);
    expect(read?.query.get('job_posting_id')).toBe(PAIR.job_posting_id);
    expect(read?.authorization).toBe(isAccount ? bearerFor(USER_A) : null);
  });

  it('with no pair says what is missing, disables the button by that reason, and calls nothing (AC-45)', () => {
    const f = stubAccountFetch(routes({ stored: null }, status(500, 'boom')));
    renderPanel(scope, null);

    expect(screen.getByText(NOT_READY)).toBeInTheDocument();
    const button = checkButton();
    expect(button).toBeDisabled();
    expect(button).toHaveAccessibleDescription(NOT_READY);
    expect(f.calls).toHaveLength(0);
  });

  it('while the read has no answer shows a Loading status and no button, so nothing can be sent twice', async () => {
    stubAccountFetch({
      ...routes({ stored: null }, hang),
      'GET /api/fit-scores': hang,
      'GET /api/me/fit-scores': hang,
    });
    renderPanel(scope);

    expect(await screen.findByRole('status')).toHaveTextContent('Loading…');
    expect(screen.queryByRole('button', { name: CHECK_BUTTON })).not.toBeInTheDocument();
  });

  it('when the read fails shows an alert with Retry, and Retry re-reads (a read is safe to repeat)', async () => {
    const f = stubAccountFetch({
      ...routes({ stored: null }, status(500, 'boom')),
      'GET /api/fit-scores': status(422, 'validation_error'),
      'GET /api/me/fit-scores': status(422, 'validation_error'),
    });
    renderPanel(scope);

    expect(await screen.findByRole('alert')).toHaveTextContent(LOAD_ERROR);
    expect(posts(f)).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: RETRY_LOAD }));
    await vi.waitFor(() => {
      expect(gets(f)).toHaveLength(2);
    });
  });

  it('empty: the notice reads exactly, and is the button’s accessible description (AC-47)', async () => {
    stubAccountFetch(routes({ stored: null }, status(500, 'boom')));
    renderPanel(scope);

    const button = await screen.findByRole('button', { name: CHECK_BUTTON });
    expect(button).toBeEnabled();
    expect(screen.getByText(FIT_SCORE_NOTICE)).toBeInTheDocument();
    expect(button).toHaveAccessibleDescription(FIT_SCORE_NOTICE);
  });

  it('a stored score shows with no button and no request: the number, estimate, gaps, retention (AC-45)', async () => {
    const f = stubAccountFetch(routes({ stored: SCORE }, status(500, 'boom')));
    renderPanel(scope);

    expect(await screen.findByText('62%')).toBeInTheDocument();
    expect(screen.getByText(ESTIMATE_LABEL)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: GAPS_HEADING, level: 3 })).toBeInTheDocument();
    const items = screen.getAllByRole('listitem').map((li) => li.textContent);
    expect(items).toEqual(['Kubernetes', 'Terraform']);
    expect(screen.getByText(retention)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: CHECK_BUTTON })).not.toBeInTheDocument();
    expect(posts(f)).toHaveLength(0);
  });

  it('shows the other scope’s retention sentence nowhere', async () => {
    stubAccountFetch(routes({ stored: SCORE }, status(500, 'boom')));
    renderPanel(scope);
    await screen.findByText('62%');

    expect(
      screen.queryByText(isAccount ? GUEST_RETENTION : ACCOUNT_RETENTION),
    ).not.toBeInTheDocument();
  });

  it('with no gaps says there is no clear gap instead of an empty list', async () => {
    stubAccountFetch(routes({ stored: { ...SCORE, gaps: [] } }, status(500, 'boom')));
    renderPanel(scope);

    expect(await screen.findByText(NO_CLEAR_GAP)).toBeInTheDocument();
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
  });

  it('renders a gap as text, never as markup', async () => {
    stubAccountFetch(
      routes({ stored: { ...SCORE, gaps: ['<b>Kubernetes</b>'] } }, status(500, 'boom')),
    );
    renderPanel(scope);

    const item = await screen.findByRole('listitem');
    expect(item.textContent).toBe('<b>Kubernetes</b>');
    expect(item.querySelector('b')).toBeNull();
  });

  it('a click sends one POST of the pair to the scope path, then shows the score (AC-45)', async () => {
    const world: World = { stored: null };
    const f = stubAccountFetch(
      routes(world, () => {
        world.stored = SCORE;
        return jsonResponse(201, SCORE);
      }),
    );
    renderPanel(scope);
    fireEvent.click(await screen.findByRole('button', { name: CHECK_BUTTON }));

    expect(await screen.findByText('62%')).toBeInTheDocument();
    const sent = posts(f);
    expect(sent).toHaveLength(1);
    expect(sent[0]?.path).toBe(path);
    expect(sent[0]?.body).toEqual(PAIR);
    expect(sent[0]?.authorization).toBe(isAccount ? bearerFor(USER_A) : null);
  });

  it('while the POST is in flight shows the working status and a disabled button, not a failure (AC-46)', async () => {
    stubAccountFetch(routes({ stored: null }, hang));
    renderPanel(scope);
    fireEvent.click(await screen.findByRole('button', { name: CHECK_BUTTON }));

    expect(await screen.findByRole('status')).toHaveTextContent(WORKING);
    expect(screen.getByRole('button', { name: CHECK_BUTTON })).toBeDisabled();
    expect(screen.queryByText(FAILED)).not.toBeInTheDocument();
  });

  it('a double click in one tick sends one request (AC-48)', async () => {
    const f = stubAccountFetch(routes({ stored: null }, hang));
    renderPanel(scope);
    const button = await screen.findByRole('button', { name: CHECK_BUTTON });
    fireEvent.click(button);
    fireEvent.click(button);
    await act(async () => {
      await Promise.resolve();
    });

    expect(posts(f)).toHaveLength(1);
  });

  it('a failed POST shows the failure, the notice and Try again; Try again sends a second POST (AC-45)', async () => {
    const f = stubAccountFetch(routes({ stored: null }, status(503, 'scoring_failed')));
    renderPanel(scope);
    fireEvent.click(await screen.findByRole('button', { name: CHECK_BUTTON }));

    expect(await screen.findByText(FAILED)).toBeInTheDocument();
    expect(screen.getByText(FIT_SCORE_NOTICE)).toBeInTheDocument();
    expect(posts(f)).toHaveLength(1);
    const again = screen.getByRole('button', { name: TRY_AGAIN });
    expect(again).toBeEnabled();
    fireEvent.click(again);
    await vi.waitFor(() => {
      expect(posts(f)).toHaveLength(2);
    });
  });

  it('a POST lost on the wire after the server finished shows the stored score, not the failure (AC-46)', async () => {
    const world: World = { stored: null };
    stubAccountFetch(
      routes(world, () => {
        world.stored = SCORE; // the server finished; the answer never arrived
        return jsonResponse(503, { error: { code: 'service_unavailable', message: 'gone' } });
      }),
    );
    renderPanel(scope);
    fireEvent.click(await screen.findByRole('button', { name: CHECK_BUTTON }));

    expect(await screen.findByText('62%')).toBeInTheDocument();
    expect(screen.queryByText(FAILED)).not.toBeInTheDocument();
  });

  it.each([
    ['scoring_refused', REFUSED.scoring_refused],
    ['inputs_too_large', REFUSED.inputs_too_large],
  ] as const)(
    'refused (%s) says why and offers no button, since the same pair would be refused again',
    async (code, sentence) => {
      stubAccountFetch(routes({ stored: null }, status(422, code)));
      renderPanel(scope);
      fireEvent.click(await screen.findByRole('button', { name: CHECK_BUTTON }));

      expect(await screen.findByText(sentence)).toBeInTheDocument();
      expect(screen.queryByRole('button')).not.toBeInTheDocument();
    },
  );

  it(`busy (AC-15): ${isAccount ? 'says busy, with no sign-up link' : 'says busy and signing up still works, with a link to /register'}`, async () => {
    stubAccountFetch(routes({ stored: null }, status(503, 'scoring_busy')));
    renderPanel(scope);
    fireEvent.click(await screen.findByRole('button', { name: CHECK_BUTTON }));

    expect(await screen.findByText(isAccount ? ACCOUNT_BUSY : GUEST_BUSY)).toBeInTheDocument();
    const link = screen.queryByRole('link', { name: SIGN_UP_LINK });
    if (isAccount) {
      expect(link).not.toBeInTheDocument();
    } else {
      expect(link).toHaveAttribute('href', '/register');
    }
  });

  it('a 429 holds the button disabled with a sentence naming the wait, and sends nothing more (the hold)', async () => {
    const f = stubAccountFetch(
      routes({ stored: null }, () =>
        jsonResponse(
          429,
          { error: { code: 'rate_limited', message: 'Too many.' } },
          { 'Retry-After': '120' },
        ),
      ),
    );
    renderPanel(scope);
    fireEvent.click(await screen.findByRole('button', { name: CHECK_BUTTON }));

    expect(
      await screen.findByText(
        /^You can try again (in \d+ seconds?|at \d{1,2}:\d{2}(?:\s?[AP]M)?)\.$/,
      ),
    ).toBeInTheDocument();
    const button = screen.getByRole('button');
    expect(button).toBeDisabled();
    fireEvent.click(button);
    await act(async () => {
      await Promise.resolve();
    });
    expect(posts(f)).toHaveLength(1);
  });

  it('a released hold gives the button back, enabled, with the notice (the hold)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    stubAccountFetch(
      routes({ stored: null }, () =>
        jsonResponse(
          429,
          { error: { code: 'rate_limited', message: 'Too many.' } },
          { 'Retry-After': '5' },
        ),
      ),
    );
    renderPanel(scope);
    fireEvent.click(await screen.findByRole('button', { name: CHECK_BUTTON }));
    await vi.waitFor(() => {
      expect(screen.getByRole('button')).toBeDisabled();
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });

    expect(screen.getByRole('button', { name: CHECK_BUTTON })).toBeEnabled();
    expect(screen.getByText(FIT_SCORE_NOTICE)).toBeInTheDocument();
  });
});

describe('FitScorePanel (guest only)', () => {
  it('a 401 guest_session_expired says the session ended and offers no retry', async () => {
    stubAccountFetch(routes({ stored: null }, status(401, 'guest_session_expired')));
    renderPanel(GUEST);
    fireEvent.click(await screen.findByRole('button', { name: CHECK_BUTTON }));

    expect(await screen.findByText(SESSION_EXPIRED)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: TRY_AGAIN })).not.toBeInTheDocument();
    expect(screen.queryByText(FAILED)).not.toBeInTheDocument();
  });

  it('limit: gives the 3-check sentence and a link to /register (AC-45, OQ-9)', async () => {
    stubAccountFetch(
      routes(
        { stored: null },
        status(409, 'too_many_fit_scores', 'Limit.', { limit: 3, retry_not_before: null }),
      ),
    );
    renderPanel(GUEST);
    fireEvent.click(await screen.findByRole('button', { name: CHECK_BUTTON }));

    expect(await screen.findByText(GUEST_LIMIT)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: SIGN_UP_LINK })).toHaveAttribute('href', '/register');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});

describe('FitScorePanel (account only)', () => {
  // Local-time constructors, so the test holds in any zone. Date only is faked: promises and
  // TanStack's timers stay real.
  const NOW = new Date(2026, 10, 1, 10, 0, 0);

  async function reachLimit(retryNotBefore: Date): Promise<void> {
    vi.useFakeTimers({ toFake: ['Date'], now: NOW });
    stubAccountFetch(
      routes(
        { stored: null },
        status(409, 'too_many_fit_scores', 'Limit.', {
          limit: 10,
          retry_not_before: retryNotBefore.toISOString(),
        }),
      ),
    );
    renderPanel(ACCOUNT);
    fireEvent.click(await screen.findByRole('button', { name: CHECK_BUTTON }));
  }

  it('limit today: "You can check again at HH:MM" without a day word', async () => {
    await reachLimit(new Date(2026, 10, 1, 14, 0, 0));

    const sentence = await screen.findByText(/you can check again/i);
    expect(sentence.textContent).toMatch(/^You can check again at \d{1,2}:\d{2}/);
    expect(sentence.textContent).not.toMatch(/tomorrow/i);
    expect(screen.queryByRole('link', { name: SIGN_UP_LINK })).not.toBeInTheDocument();
  });

  it('limit on another day: names the day ("tomorrow at HH:MM")', async () => {
    await reachLimit(new Date(2026, 10, 2, 9, 0, 0));

    const sentence = await screen.findByText(/you can check again/i);
    expect(sentence.textContent).toMatch(/^You can check again tomorrow at \d{1,2}:\d{2}/);
  });

  it.each([
    [403, 'account_disabled'],
    [401, 'not_signed_in'],
  ] as const)(
    'a %i %s on the POST takes the existing path and signs the tab out',
    async (code, error) => {
      stubAccountFetch(routes({ stored: null }, status(code, error)));
      renderPanel(ACCOUNT);
      fireEvent.click(await screen.findByRole('button', { name: CHECK_BUTTON }));

      await vi.waitFor(() => {
        expect(authStore.getSnapshot().status).toBe('anonymous');
      });
    },
  );

  it('does not sign out on a 503, the positive control for the sign-out tests', async () => {
    stubAccountFetch(routes({ stored: null }, status(503, 'scoring_failed')));
    renderPanel(ACCOUNT);
    fireEvent.click(await screen.findByRole('button', { name: CHECK_BUTTON }));

    await screen.findByText(FAILED);
    expect(authStore.getSnapshot().status).toBe('authenticated');
  });
});
