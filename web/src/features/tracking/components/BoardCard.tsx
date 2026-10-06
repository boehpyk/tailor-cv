import { memo, useCallback, useId } from 'react';
import { Link } from 'react-router';

import { runLink } from '@/features/scope/scopeMap';
import { useScopeMap } from '@/features/scope/useWorkspaceScope';

import { CardTitleEditor } from './CardTitleEditor';
import { MoveToControl } from './MoveToControl';
import {
  OPEN_DOCUMENTS_LABEL,
  OPEN_POSTING_LABEL,
  REMOVE_FAILED_NOTE,
  REMOVE_LABEL,
  REMOVE_PENDING_LABEL,
  cardCvLine,
  cardDisplayTitle,
  sinceCopy,
} from '../trackingCopy';

import type { CardDragProps } from '../hooks/useCardDrag';
import type { BoardCard as BoardCardData, Stage } from '../types';

/**
 * Where the card's removal stands — the page derives it from the untrack mutation, never a copy of
 * it (AC-37).
 */
export type CardRemoval =
  { readonly kind: 'idle' } | { readonly kind: 'removing' } | { readonly kind: 'failed' };

/** The three removals as constants: a page hands these, never a fresh literal, so `memo` holds. */
export const CARD_REMOVAL = {
  idle: { kind: 'idle' },
  removing: { kind: 'removing' },
  failed: { kind: 'failed' },
} as const satisfies Record<CardRemoval['kind'], CardRemoval>;

export interface BoardCardProps {
  /** The signed-in user — `CardTitleEditor`'s mutation key is rooted on it. */
  readonly userId: string;
  readonly card: BoardCardData;
  /** The card's move is on the wire: the Move control reads *"Saving…"*, disabled (T-39). */
  readonly movePending: boolean;
  /**
   * Move a card to `stage` — the page's optimistic mutation (AC-33). Takes the card's id, so the
   * page hands every card the **same** function and `memo` can skip the cards a move did not touch.
   */
  readonly onMove: (cardId: string, stage: Stage) => void;
  /** `useMoveAnnouncer().moveControlRef(card.id)` — so focus can follow the card (AC-33). */
  readonly moveControlRef: React.RefCallback<HTMLSelectElement>;
  /** One of `CARD_REMOVAL`'s constants, so an unchanged removal is the same object (`memo`). */
  readonly removal: CardRemoval;
  /** Remove a card from the board — one function for every card, like `onMove`. */
  readonly onRemove: (cardId: string) => void;
  /** `useCardDrag().cardProps(card)` — the same object for the same id and stage. */
  readonly dragProps: CardDragProps;
}

const LINK_CLASS = 'font-medium text-slate-900 underline underline-offset-2';

/**
 * The posting's own page, only when it is `http://` or `https://` (AC-40). The server validated it
 * on capture (`PostingUrl`); this is the second lock, because a `javascript:` URL in an `href` is a
 * script, and React 19 only *warns* about one.
 */
function safePostingUrl(card: BoardCardData): string | null {
  const url = card.posting?.source_url ?? null;
  return url !== null && /^https?:\/\//i.test(url) ? url : null;
}

/**
 * One card — **presentational** (AC-32): the display title (title → posting title → preview), the CV
 * line (label → filename → *"CV deleted"*), *"since {date}"*, a link to `/history/{runId}/cv`
 * (built with `runLink`, never by hand), the posting URL as an external link (`rel="noopener
 * noreferrer"`, `target="_blank"`, `http(s)` only), then `MoveToControl`, `CardTitleEditor` and
 * **Remove from board** (*"Removing…"* while pending, *"Not removed — try again."* on failure).
 *
 * Every string from a user or the posting is a text node: React escapes it, and nothing here builds
 * markup from it. Each control is described by the card's title, so a screen reader hears *which*
 * card's "Move to" it is on.
 *
 * A card whose run row is missing (T-36) has no documents to open, so it gets no link; it can still
 * be moved, retitled and removed.
 *
 * **Memoized** (AC-44: an optimistic move commits in ≤ 50 ms at 500 cards). A move rewrites one
 * card in the cache entry and keeps every other card object as it was, and every other prop here is
 * stable per card (the page's callbacks take the card id; the ref and drag props are cached per id),
 * so a move re-renders the moved card and nothing else — not all 500.
 */
export const BoardCard = memo(function BoardCard({
  userId,
  card,
  movePending,
  onMove,
  moveControlRef,
  removal,
  onRemove,
  dragProps,
}: BoardCardProps): React.JSX.Element {
  const titleId = useId();
  const removeNoteId = useId();
  const scope = useScopeMap();
  const postingUrl = safePostingUrl(card);
  const cardId = card.id;
  const moveThis = useCallback(
    (stage: Stage) => {
      onMove(cardId, stage);
    },
    [onMove, cardId],
  );
  const removeThis = useCallback(() => {
    onRemove(cardId);
  }, [onRemove, cardId]);

  return (
    <li
      // Where focus-after-move looks for "still inside this card" (`useMoveAnnouncer`).
      data-board-card=""
      {...dragProps}
      className="space-y-2 rounded-md border border-slate-200 bg-white p-3 shadow-sm"
    >
      <p id={titleId} className="font-medium break-words text-slate-900">
        {cardDisplayTitle(card)}
      </p>
      <p className="text-sm text-slate-600">
        <span className="text-slate-500">CV: </span>
        <span>{cardCvLine(card)}</span>
      </p>
      <p className="text-xs text-slate-500">
        <time dateTime={card.stage_changed_at}>{sinceCopy(card.stage_changed_at)}</time>
      </p>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-sm">
        {card.run !== null && (
          <Link to={runLink(scope, card.tailoring_run_id, 'cv')} className={LINK_CLASS}>
            {OPEN_DOCUMENTS_LABEL}
          </Link>
        )}
        {postingUrl !== null && (
          <a href={postingUrl} target="_blank" rel="noopener noreferrer" className={LINK_CLASS}>
            {OPEN_POSTING_LABEL}
          </a>
        )}
      </div>
      <MoveToControl
        stage={card.stage}
        pending={movePending}
        onMove={moveThis}
        ref={moveControlRef}
        describedBy={titleId}
      />
      <CardTitleEditor
        userId={userId}
        applicationId={card.id}
        title={card.title}
        version={card.version}
        movePending={movePending}
        describedBy={titleId}
      />
      <div className="text-sm">
        <button
          type="button"
          onClick={removeThis}
          disabled={removal.kind === 'removing'}
          aria-describedby={removal.kind === 'failed' ? `${titleId} ${removeNoteId}` : titleId}
          className="text-red-700 underline underline-offset-2 disabled:text-slate-400 disabled:no-underline"
        >
          {removal.kind === 'removing' ? REMOVE_PENDING_LABEL : REMOVE_LABEL}
        </button>
        {removal.kind === 'failed' && (
          <p id={removeNoteId} role="alert" className="mt-1 text-slate-700">
            {REMOVE_FAILED_NOTE}
          </p>
        )}
      </div>
    </li>
  );
});
