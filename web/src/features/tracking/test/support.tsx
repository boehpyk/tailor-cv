import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';

import { WorkspaceScopeProvider } from '@/features/scope/WorkspaceScope';
import { USER_A, USER_B, signedInRoutes } from '@/test/accountFetch';
import { jsonResponse } from '@/test/fixtures';

import { BoardPage } from '../components/BoardPage';

import type { RecordedCall, RouteHandler } from '@/test/accountFetch';
import type { BoardCard, Stage, TrackedApplication } from '../types';
import type { RenderResult } from '@testing-library/react';

/**
 * Slice 3.1 test support (T27): the board's fixtures, a **stateful fake of the board routes** and the
 * renderers every tracking test shares.
 *
 * **The fake has the isolation of what it fakes** (CLAUDE.md, 2.4's `ed66fbe`): the board is keyed by
 * the bearer, so a request carrying B's token can never read or move A's card, and a write changes
 * what the next `GET /api/me/board` returns — the same loop the real API closes. It also honours the
 * version check (409 on a stale `version`), because a fake that accepted any version would let a
 * client that forgot to send one pass.
 *
 * **The client keeps what it is given**: `gcTime: Infinity`. `gcTime: 0` garbage-collects an
 * unobserved `setQueryData` entry on the next macrotask, so an "it is gone" or "it was kept"
 * assertion would test the collector, not the code (CLAUDE.md, 2.1's trap).
 */

export const BOARD_PATH = '/api/me/board';
export const APPLICATIONS_PATH = '/api/me/tracked-applications';

export function newClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
  });
}

export function makeCard(overrides: Partial<BoardCard> = {}): BoardCard {
  return {
    id: 'app-1',
    tailoring_run_id: 'run-1',
    stage: 'to_apply',
    title: 'Platform role at Acme',
    tracked_at: '2026-09-20T10:00:00Z',
    stage_changed_at: '2026-09-21T10:00:00Z',
    version: 3,
    run: { requested_at: '2026-09-20T09:00:00Z', edited: false },
    posting: {
      job_posting_id: 'posting-1',
      source: 'pasted',
      title: 'Senior Platform Engineer',
      source_url: null,
      preview: 'We are looking for a platform engineer…',
    },
    base_cv: { base_cv_id: 'cv-1', label: 'Backend roles', original_filename: 'jane.pdf' },
    ...overrides,
  };
}

/** Another card, distinct in id, run and title, so two cards on one board can be told apart. */
export function otherCard(n: number, overrides: Partial<BoardCard> = {}): BoardCard {
  return makeCard({
    id: `app-${String(n)}`,
    tailoring_run_id: `run-${String(n)}`,
    title: `Card number ${String(n)}`,
    ...overrides,
  });
}

export function toApplication(card: BoardCard): TrackedApplication {
  return {
    id: card.id,
    tailoring_run_id: card.tailoring_run_id,
    stage: card.stage,
    title: card.title,
    tracked_at: card.tracked_at,
    stage_changed_at: card.stage_changed_at,
    version: card.version,
  };
}

export function apiError(
  status: number,
  code: string,
  extra: object = {},
  headers?: Record<string, string>,
): Response {
  return jsonResponse(status, { error: { code, message: `fixture: ${code}`, ...extra } }, headers);
}

export interface Deferred<T> {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
}

export function deferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function userIdOf(call: RecordedCall): string {
  const match = /^Bearer token-(user-[a-z])/.exec(call.authorization ?? '');
  return match?.[1] ?? 'nobody';
}

/** The segment after `/api/me/tracked-applications/`. */
function applicationIdOf(call: RecordedCall): string {
  return call.path.split('/')[4] ?? '';
}

export interface BoardServer {
  /** One user's cards, live: a write below changes what the next read returns. */
  readonly cardsOf: (userId: string) => BoardCard[];
  readonly getBoard: RouteHandler;
  readonly move: RouteHandler;
  readonly retitle: RouteHandler;
  readonly untrack: RouteHandler;
  readonly track: RouteHandler;
  /** The routes a signed-in board page needs; spread it, then override one entry per test. */
  readonly routes: () => Record<string, RouteHandler>;
}

export function boardServer(initial: Readonly<Record<string, readonly BoardCard[]>>): BoardServer {
  const boards = new Map<string, BoardCard[]>(
    Object.entries(initial).map(([userId, cards]) => [userId, [...cards]]),
  );
  const cardsOf = (userId: string): BoardCard[] => {
    const existing = boards.get(userId);
    if (existing !== undefined) {
      return existing;
    }
    const fresh: BoardCard[] = [];
    boards.set(userId, fresh);
    return fresh;
  };
  const replace = (userId: string, next: BoardCard): void => {
    const cards = cardsOf(userId);
    cards.splice(
      cards.findIndex((card) => card.id === next.id),
      1,
      next,
    );
  };
  const notFound = (): Response => apiError(404, 'tracked_application_not_found');
  const conflict = (current: number): Response =>
    apiError(409, 'tracked_application_version_conflict', { current_version: current });

  const getBoard: RouteHandler = (call) => jsonResponse(200, { items: cardsOf(userIdOf(call)) });

  const move: RouteHandler = (call) => {
    const userId = userIdOf(call);
    const card = cardsOf(userId).find((candidate) => candidate.id === applicationIdOf(call));
    const body = call.body as { stage: Stage; version: number };
    if (card === undefined) {
      return notFound();
    }
    if (card.version !== body.version) {
      return conflict(card.version);
    }
    if (card.stage === body.stage) {
      return jsonResponse(200, toApplication(card));
    }
    const moved: BoardCard = {
      ...card,
      stage: body.stage,
      stage_changed_at: '2026-10-05T12:00:00Z',
      version: card.version + 1,
    };
    replace(userId, moved);
    return jsonResponse(200, toApplication(moved));
  };

  const retitle: RouteHandler = (call) => {
    const userId = userIdOf(call);
    const card = cardsOf(userId).find((candidate) => candidate.id === applicationIdOf(call));
    const body = call.body as { title: string | null; version: number };
    if (card === undefined) {
      return notFound();
    }
    if (card.version !== body.version) {
      return conflict(card.version);
    }
    const next: BoardCard = {
      ...card,
      title: body.title === null ? null : body.title.trim(),
      version: card.version + 1,
    };
    replace(userId, next);
    return jsonResponse(200, toApplication(next));
  };

  const untrack: RouteHandler = (call) => {
    const userId = userIdOf(call);
    const cards = cardsOf(userId);
    const at = cards.findIndex((candidate) => candidate.id === applicationIdOf(call));
    if (at === -1) {
      return notFound();
    }
    cards.splice(at, 1);
    return new Response(null, { status: 204 });
  };

  const track: RouteHandler = (call) => {
    const userId = userIdOf(call);
    const runId = (call.body as { tailoring_run_id: string }).tailoring_run_id;
    const existing = cardsOf(userId).find((card) => card.tailoring_run_id === runId);
    if (existing !== undefined) {
      return apiError(409, 'application_already_tracked', { tracked_application_id: existing.id });
    }
    const created = makeCard({
      id: `app-for-${runId}`,
      tailoring_run_id: runId,
      title: null,
      stage: 'to_apply',
      version: 1,
    });
    cardsOf(userId).push(created);
    return jsonResponse(201, toApplication(created));
  };

  const routes = (): Record<string, RouteHandler> => ({
    ...signedInRoutes(USER_A),
    'GET /api/auth/me': (call) => jsonResponse(200, userIdOf(call) === 'user-b' ? USER_B : USER_A),
    [`GET ${BOARD_PATH}`]: getBoard,
    [`PUT ${APPLICATIONS_PATH}/:id/stage`]: move,
    [`PUT ${APPLICATIONS_PATH}/:id/title`]: retitle,
    [`DELETE ${APPLICATIONS_PATH}/:id`]: untrack,
    [`POST ${APPLICATIONS_PATH}`]: track,
  });

  return { cardsOf, getBoard, move, retitle, untrack, track, routes };
}

export interface RenderedBoard extends RenderResult {
  readonly queryClient: QueryClient;
}

/** The board page for `userId`, in account scope, as the route would mount it. */
export function renderBoard(
  userId = 'user-a',
  queryClient: QueryClient = newClient(),
): RenderedBoard {
  const result = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <WorkspaceScopeProvider scope={{ kind: 'account', userId }}>
          <BoardPage userId={userId} />
        </WorkspaceScopeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, queryClient };
}

/** A stage's column — the labelled region whose accessible name starts with the stage's label. */
export function regionOf(label: string): HTMLElement {
  return screen.getByRole('region', { name: new RegExp(`^${label}\\b`) });
}

/**
 * The card whose visible text is `text` — found by climbing from that text to the first ancestor
 * that holds exactly one *Remove from board* button, so the helper says nothing about the card's
 * markup (a `li`, an `article`, a `div`).
 */
export function cardWithText(text: string): HTMLElement {
  let node: HTMLElement | null = screen.getByText(text);
  while (node !== null) {
    if (within(node).queryAllByRole('button', { name: /remove from board/i }).length === 1) {
      return node;
    }
    node = node.parentElement;
  }
  throw new Error(`no card contains ${JSON.stringify(text)}`);
}

export function moveControlOf(card: HTMLElement): HTMLElement {
  return within(card).getByRole('combobox', { name: /move to/i });
}
