import { useMutationState, useQueryClient } from '@tanstack/react-query';
import { useCallback, useId, useMemo, useState } from 'react';
import { Link } from 'react-router';

import { holdDeadline } from '@/features/retry/hold';

import { BoardCard, CARD_REMOVAL } from './BoardCard';
import { BoardColumn } from './BoardColumn';
import { variablesNameId } from '../hooks/boardCache';
import {
  boardKey,
  moveTrackedApplicationMutationKey,
  untrackApplicationMutationKey,
} from '../hooks/trackingKeys';
import { useBoard } from '../hooks/useBoard';
import { useCardDrag } from '../hooks/useCardDrag';
import { useMoveAnnouncer } from '../hooks/useMoveAnnouncer';
import { useMoveTrackedApplication } from '../hooks/useMoveTrackedApplication';
import { useUntrackApplication } from '../hooks/useUntrackApplication';
import {
  BOARD_EMPTY_ACTION,
  BOARD_EMPTY_NOTE,
  BOARD_HEADING,
  BOARD_LOADING_NOTE,
  BOARD_LOAD_ERROR_NOTE,
  BOARD_RETENTION_NOTE,
  REMOVED_NOTE,
  RETRY_LABEL,
  cardDisplayTitle,
  moveFailureCopy,
  movedAnnouncement,
} from '../trackingCopy';
import { STAGES } from '../types';

import type { CardRemoval } from './BoardCard';
import type { Board, BoardCard as BoardCardData, Stage } from '../types';

export interface BoardPageProps {
  /** The signed-in user — handed down by `AccountScope`, and the root of every tracking key. */
  readonly userId: string;
}

const BUTTON_CLASS =
  'min-h-11 rounded-lg bg-brand-600 px-4 text-sm font-semibold text-white hover:bg-brand-700 disabled:opacity-60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 sm:min-h-9';

/**
 * `/board` — a **container** (AC-32), under `RequireAuth` and `AccountScope`.
 *
 * - **Loading:** `role="status"`, *"Loading your board…"*.
 * - **Error:** `role="alert"`, *"Couldn't load your board"* + **Retry** — never the empty state.
 * - **Empty:** *"Nothing on your board yet"* + a link to `/history`.
 * - **Success:** six `BoardColumn`s in `STAGES` order, each a labelled region with its count;
 *   a `BoardCard` per card.
 *
 * The retention note sits under the heading in every state: it is a promise about the data, not
 * about this screen.
 *
 * The page is a `div`, not a `section`: a named `section` is itself a `region`, and the six columns
 * are meant to be the page's only regions, in board order (AC-40).
 */
export function BoardPage({ userId }: BoardPageProps): React.JSX.Element {
  const headingId = useId();
  return (
    <div className="mb-10 space-y-4">
      <div className="space-y-1">
        <h2
          id={headingId}
          className="font-display text-2xl font-bold tracking-tight text-slate-900 sm:text-3xl"
        >
          {BOARD_HEADING}
        </h2>
        <p className="text-sm text-slate-600">{BOARD_RETENTION_NOTE}</p>
      </div>
      <BoardBody userId={userId} />
    </div>
  );
}

/**
 * The board's body. It owns the move and untrack mutations, `useCardDrag` and `useMoveAnnouncer`,
 * and holds exactly one piece of local state: the sentence explaining the last refused move (AC-34).
 * That is a notice, not server data — the card itself is already back where the server has it.
 *
 * **Which cards are busy** is read from the mutation cache (`useMutationState`), not from the
 * `useMutation` observer, which follows only the latest call: a second card moved while the first is
 * on the wire must not make the first look idle (T-39).
 *
 * The polite live region is rendered in every state, so the *"Removed…"* note survives the last
 * card's removal turning the board empty.
 *
 * A removal's *"Removing…"* / *"Not removed"* is read from the untrack observer's `variables`,
 * `isPending` and `isError` — the latest removal only. That is enough there: the button is disabled
 * while its card is removing, and a failed removal leaves the card where it was, so nothing moves
 * silently.
 */
function BoardBody({ userId }: BoardPageProps): React.JSX.Element {
  const view = useBoard(userId);
  const queryClient = useQueryClient();
  const announcer = useMoveAnnouncer();
  const [moveFailure, setMoveFailure] = useState<string | null>(null);
  const moveMutationKey = moveTrackedApplicationMutationKey(userId);

  const { announceMove, announce, returnFocus } = announcer;
  const move = useMoveTrackedApplication(userId, {
    onMoved: (card, stage) => {
      announceMove(card.id, movedAnnouncement(cardDisplayTitle(card), stage));
    },
    onRefused: (error, card) => {
      // Worded here, once, at the refusal (AC-16): the note is state, so it never ticks.
      const now = Date.now();
      setMoveFailure(moveFailureCopy(error, holdDeadline(error, now), now));
      // The card is back where it was, but the control the user chose from was unmounted by the
      // optimistic move: hand focus back to it. A card that is gone (404) has no control to return to.
      if (card.restored) {
        returnFocus(card.id);
      }
    },
  });
  const untrack = useUntrackApplication(userId, {
    onRemoved: (outcome) => {
      if (outcome === 'removed') {
        announce(REMOVED_NOTE);
      }
    },
  });

  const movingIds = useMutationState({
    filters: { mutationKey: moveMutationKey, status: 'pending' },
    select: (mutation) => mutation.state.variables,
  });

  // The ids of the cards whose move is on the wire, as a set the cards read a boolean from.
  const movingIdSet = useMemo(() => {
    const ids = new Set<string>();
    for (const variables of movingIds) {
      if (typeof variables === 'object' && variables !== null && 'id' in variables) {
        const { id } = variables;
        if (typeof id === 'string') {
          ids.add(id);
        }
      }
    }
    return ids;
  }, [movingIds]);

  // **Stable across a move** (AC-44): every card gets this one function, so a memoized `BoardCard`
  // is not re-rendered because its callback is new. The card is read from the cache entry at call
  // time — the same entry `useBoard` renders — rather than closed over, which would make the
  // function change with every move.
  const { mutate: mutateMove } = move;
  const moveCard = useCallback(
    (id: string, stage: Stage) => {
      const card = queryClient
        .getQueryData<Board>(boardKey(userId))
        ?.items.find((candidate) => candidate.id === id);
      if (card === undefined || card.stage === stage) {
        return;
      }
      // `isPending` lags a same-tick double choice; the mutation cache does not (T-29). Scoped to
      // this card: another card's move is not a reason to drop this one.
      const inFlight = queryClient.isMutating({
        mutationKey: moveTrackedApplicationMutationKey(userId),
        predicate: (mutation) => variablesNameId(mutation.state.variables, id),
      });
      if (inFlight > 0) {
        return;
      }
      setMoveFailure(null);
      mutateMove({ id, stage, version: card.version });
    },
    [queryClient, userId, mutateMove],
  );
  const drag = useCardDrag(moveCard);

  const { mutate: mutateUntrack } = untrack;
  const removeCard = useCallback(
    (id: string) => {
      const inFlight = queryClient.isMutating({
        mutationKey: untrackApplicationMutationKey(userId),
        predicate: (mutation) => variablesNameId(mutation.state.variables, id),
      });
      if (inFlight === 0) {
        mutateUntrack(id);
      }
    },
    [queryClient, userId, mutateUntrack],
  );

  const liveRegion = (
    <p aria-live="polite" aria-atomic="true" className="min-h-5 text-sm text-slate-600">
      {announcer.message}
    </p>
  );
  // A refused move's reason, near the board — also when a 404 took the last card and the board is
  // now empty, so the reason is never lost with the card (AC-34).
  const failureNotice = moveFailure !== null && (
    <p role="alert" className="text-sm font-medium text-danger-600">
      {moveFailure}
    </p>
  );

  if (view.status === 'loading') {
    return (
      <>
        <p role="status" className="text-sm text-slate-600">
          {BOARD_LOADING_NOTE}
        </p>
        {liveRegion}
      </>
    );
  }
  if (view.status === 'error') {
    return (
      <>
        <div
          role="alert"
          className="space-y-3 rounded-xl border border-danger-200 bg-danger-50 p-4"
        >
          <p className="text-sm font-medium text-slate-900">{BOARD_LOAD_ERROR_NOTE}</p>
          <button type="button" onClick={view.retry} className={BUTTON_CLASS}>
            {RETRY_LABEL}
          </button>
        </div>
        {liveRegion}
      </>
    );
  }
  if (view.cards.length === 0) {
    return (
      <>
        {liveRegion}
        {failureNotice}
        <p className="rounded-xl border-2 border-dashed border-slate-300 bg-white p-6 text-sm text-slate-700">
          <span className="font-semibold text-slate-900">{BOARD_EMPTY_NOTE}</span>{' '}
          <Link
            to="/history"
            className="rounded-sm font-semibold text-brand-700 underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600"
          >
            {BOARD_EMPTY_ACTION}
          </Link>
        </p>
      </>
    );
  }

  // A constant, never a fresh literal: `BoardCard` is memoized (AC-44).
  function removalOf(card: BoardCardData): CardRemoval {
    if (untrack.variables !== card.id) {
      return CARD_REMOVAL.idle;
    }
    if (untrack.isPending) {
      return CARD_REMOVAL.removing;
    }
    return untrack.isError ? CARD_REMOVAL.failed : CARD_REMOVAL.idle;
  }

  return (
    <div className="space-y-3">
      {liveRegion}
      {failureNotice}
      {/* Stacked below `sm` — one column, grouped by stage (4c); a grid up to `lg`; one row of six
          from `lg`, scrolling inside its own container so the page itself never scrolls sideways
          (AC-40). */}
      <div className="grid gap-3 sm:grid-cols-2 lg:flex lg:items-start lg:overflow-x-auto lg:pb-3">
        {STAGES.map((stage) => (
          <BoardColumn
            key={stage}
            stage={stage}
            count={view.byStage[stage].length}
            isDropTarget={drag.overStage === stage}
            dropProps={drag.columnProps(stage)}
          >
            {view.byStage[stage].map((card) => (
              <BoardCard
                key={card.id}
                userId={userId}
                card={card}
                movePending={movingIdSet.has(card.id)}
                onMove={moveCard}
                moveControlRef={announcer.moveControlRef(card.id)}
                removal={removalOf(card)}
                onRemove={removeCard}
                dragProps={drag.cardProps(card)}
              />
            ))}
          </BoardColumn>
        ))}
      </div>
    </div>
  );
}
