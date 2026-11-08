import { QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetForTests } from '@/features/auth/authStore';
import { WorkspaceScopeProvider } from '@/features/scope/WorkspaceScope';
import { createAppQueryClient } from '@/queryClient';
import {
  USER_A,
  callsTo,
  hang,
  signInAs,
  status,
  stubAccountFetch,
  type AccountFetch,
  type RouteHandler,
} from '@/test/accountFetch';
import { jsonResponse } from '@/test/fixtures';

import {
  COMPARE_BUSY,
  COMPARE_BUTTON,
  COMPARE_HEADING,
  COMPARE_NOTICE,
  COMPARE_UNAVAILABLE,
  NOT_COMPARABLE,
  SIDE_FAILED,
  SIDE_LABELS,
  SIDE_SCORING,
  TRY_AGAIN,
  VERDICTS,
  deltaPoints,
} from '../fitScoreCopy';
import { ScoreComparisonPanel } from './ScoreComparisonPanel';

import type { FitScore, RunFitScores } from '../types';

/**
 * T33 RED (slice 5.2; AC-37, plan §5's `ScoreComparisonPanel` row): the panel through a fake server
 * with the real shapes (`GET .../fit-scores` is a `RunFitScores`, `POST .../fit-scores/{side}` a
 * `FitScore`). Verdict wording is read from the copy module (5.1's scan bans *higher*, so the words
 * are the module's, not literals). A `POST` is counted on the wire, never inferred from the UI.
 */

const RUN = '0192f0a1-aaaa-7000-8000-0000000000aa';

function score(percent: number, id = `score-${String(percent)}`): FitScore {
  return {
    id,
    score_percent: percent,
    gaps: [],
    scored_at: '2026-11-08T10:00:00Z',
    expires_at: null,
  };
}

const NONE: RunFitScores = {
  before: null,
  after: null,
  comparable: null,
  comparison: null,
  unavailable: null,
};

interface World {
  stored: RunFitScores;
}

const SIDE_POST = 'POST /api/me/tailoring-runs/:id/fit-scores/:side';

function routes(
  world: World,
  posts: Record<string, RouteHandler> = {},
): Record<string, RouteHandler> {
  return {
    'GET /api/me/tailoring-runs/:id/fit-scores': () => jsonResponse(200, world.stored),
    ...posts,
  };
}

/** Both sides hang, so a test can look at what the panel shows and what was sent. */
function mount(table: Record<string, RouteHandler>): AccountFetch {
  const fetch = stubAccountFetch(table);
  signInAs(USER_A);
  render(
    <QueryClientProvider client={createAppQueryClient()}>
      <MemoryRouter>
        <WorkspaceScopeProvider scope={{ kind: 'account', userId: USER_A.id }}>
          <ScoreComparisonPanel runId={RUN} />
        </WorkspaceScopeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return fetch;
}

const sidePosts = (fetch: AccountFetch, side: string): number =>
  fetch.calls.filter((c) => c.method === 'POST' && c.path.endsWith(`/fit-scores/${side}`)).length;

async function clickCompare(): Promise<void> {
  const button = await screen.findByRole('button', { name: COMPARE_BUTTON });
  await waitFor(() => {
    expect(button).toBeEnabled();
  });
  await userEvent.setup().click(button);
}

/** A POST that stores the side's score in the world, then answers it. */
function stores(world: World, side: 'before' | 'after', percent: number): RouteHandler {
  return (call) => {
    if (!call.path.endsWith(`/${side}`)) {
      return hang();
    }
    const s = score(percent);
    world.stored = { ...world.stored, [side]: s };
    return jsonResponse(201, s);
  };
}

beforeEach(() => {
  __resetForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('ScoreComparisonPanel', () => {
  it('shows the heading, the Compare button and the Gemini notice, and sends nothing on render', async () => {
    const fetch = mount(routes({ stored: NONE }));
    expect(await screen.findByRole('button', { name: COMPARE_BUTTON })).toBeEnabled();
    expect(screen.getByText(COMPARE_NOTICE)).toBeVisible();
    expect(screen.getByRole('heading', { name: COMPARE_HEADING })).toBeVisible();
    expect(callsTo(fetch, 'GET', '/api/me/tailoring-runs/:id/fit-scores')).toHaveLength(1);
    expect(sidePosts(fetch, 'before') + sidePosts(fetch, 'after')).toBe(0);
  });

  it('scores both sides, in parallel, from one click', async () => {
    const fetch = mount(routes({ stored: NONE }, { [SIDE_POST]: hang }));
    await clickCompare();
    await waitFor(() => {
      expect(sidePosts(fetch, 'before')).toBe(1);
      expect(sidePosts(fetch, 'after')).toBe(1);
    });
    expect(screen.getAllByText(SIDE_SCORING)).toHaveLength(2);
  });

  it('sends one request per side for a same-tick double click', async () => {
    const fetch = mount(routes({ stored: NONE }, { [SIDE_POST]: hang }));
    const button = await screen.findByRole('button', { name: COMPARE_BUTTON });
    await waitFor(() => {
      expect(button).toBeEnabled();
    });
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => {
      expect(sidePosts(fetch, 'before')).toBe(1);
    });
    expect(sidePosts(fetch, 'after')).toBe(1);
  });

  it('shows one side stored while the other is still scoring', async () => {
    const world: World = { stored: NONE };
    mount(routes(world, { [SIDE_POST]: stores(world, 'before', 41) }));
    await clickCompare();
    expect(await screen.findByText(/41/)).toBeVisible();
    expect(screen.getAllByText(SIDE_SCORING)).toHaveLength(1);
    expect(screen.getByText(SIDE_LABELS.after)).toBeVisible();
  });

  it('shows a failed side with Try again while the other side keeps its score', async () => {
    const world: World = { stored: NONE };
    const fetch = mount(
      routes(world, {
        [SIDE_POST]: (call, n) =>
          call.path.endsWith('/after')
            ? status(503, 'scoring_failed')()
            : stores(world, 'before', 41)(call, n),
      }),
    );
    await clickCompare();
    expect(await screen.findByText(SIDE_FAILED)).toBeVisible();
    expect(screen.getAllByText(SIDE_FAILED)).toHaveLength(1);
    expect(screen.getByText(/41/)).toBeVisible();
    expect(screen.queryByText(SIDE_SCORING)).not.toBeInTheDocument();

    await userEvent.setup().click(screen.getByRole('button', { name: TRY_AGAIN }));
    await waitFor(() => {
      expect(sidePosts(fetch, 'after')).toBe(2);
    });
    expect(sidePosts(fetch, 'before')).toBe(1);
  });

  it('sends one request for a same-tick double click on Try again', async () => {
    const world: World = { stored: { ...NONE, before: score(41) } };
    let afterPosts = 0;
    const fetch = mount(
      routes(world, {
        [SIDE_POST]: (call) => {
          if (!call.path.endsWith('/after')) {
            return hang();
          }
          afterPosts += 1;
          return afterPosts === 1 ? status(503, 'scoring_failed')() : hang();
        },
      }),
    );
    await clickCompare();
    const retry = await screen.findByRole('button', { name: TRY_AGAIN });
    fireEvent.click(retry);
    fireEvent.click(retry);
    await waitFor(() => {
      expect(sidePosts(fetch, 'after')).toBe(2);
    });
  });

  it('says scoring is busy, and that the documents are ready, when the scorer refuses', async () => {
    mount(routes({ stored: NONE }, { [SIDE_POST]: status(503, 'scoring_busy') }));
    await clickCompare();
    expect(await screen.findByText(COMPARE_BUSY)).toBeVisible();
  });

  it.each(['base_cv_deleted', 'not_succeeded'] as const)(
    'says why a %s run cannot be compared, with nothing to click',
    async (reason) => {
      mount(routes({ stored: { ...NONE, unavailable: reason } }));
      expect(await screen.findByText(COMPARE_UNAVAILABLE[reason])).toBeVisible();
      expect(screen.queryByRole('button', { name: COMPARE_BUTTON })).not.toBeInTheDocument();
    },
  );

  it('says two scores from different scorer versions cannot be compared, and shows no verdict', async () => {
    mount(
      routes({
        stored: {
          before: score(40),
          after: score(70),
          comparable: false,
          comparison: null,
          unavailable: null,
        },
      }),
    );
    expect(await screen.findByText(NOT_COMPARABLE)).toBeVisible();
    for (const verdict of Object.values(VERDICTS)) {
      expect(screen.queryByText(verdict)).not.toBeInTheDocument();
    }
  });

  it.each([
    ['higher', 8, 50, 58],
    ['about_the_same', 3, 50, 53],
    ['lower', -9, 50, 41],
  ] as const)(
    'shows the %s verdict from the server, with its delta',
    async (verdict, delta, b, a) => {
      mount(
        routes({
          stored: {
            before: score(b),
            after: score(a),
            comparable: true,
            comparison: { delta, verdict },
            unavailable: null,
          },
        }),
      );
      expect(await screen.findByText(VERDICTS[verdict])).toBeVisible();
      expect(screen.getByText(deltaPoints(delta))).toBeVisible();
      for (const other of Object.keys(VERDICTS).filter((v) => v !== verdict)) {
        expect(
          screen.queryByText(VERDICTS[other as keyof typeof VERDICTS]),
        ).not.toBeInTheDocument();
      }
      const region = screen.getByRole('heading', { name: COMPARE_HEADING }).parentElement;
      expect(region).not.toBeNull();
      expect(within(region as HTMLElement).getByText(new RegExp(String(b)))).toBeVisible();
    },
  );
});
