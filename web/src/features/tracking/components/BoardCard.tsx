/* eslint-disable @typescript-eslint/no-unused-vars -- T26 SKELETON: the props are the signature qa's T27 tests compile against; T28 uses them and deletes this line. */
import type { CardDragProps } from '../hooks/useCardDrag';
import type { BoardCard as BoardCardData, Stage } from '../types';

/**
 * Where the card's removal stands — the page derives it from the untrack mutation (`variables`,
 * `isPending`, `isError`), never a copy of it (AC-37).
 */
export type CardRemoval =
  { readonly kind: 'idle' } | { readonly kind: 'removing' } | { readonly kind: 'failed' };

export interface BoardCardProps {
  /** The signed-in user — `CardTitleEditor`'s mutation key is rooted on it. */
  readonly userId: string;
  readonly card: BoardCardData;
  /** The card's move is on the wire: the Move control reads *"Saving…"*, disabled (T-39). */
  readonly movePending: boolean;
  /** Move this card to `stage` — the page's optimistic mutation (AC-33). */
  readonly onMove: (stage: Stage) => void;
  /** `useMoveAnnouncer().moveControlRef(card.id)` — so focus can follow the card (AC-33). */
  readonly moveControlRef: (element: HTMLSelectElement | null) => void;
  readonly removal: CardRemoval;
  readonly onRemove: () => void;
  /** `useCardDrag().cardProps(card)`. */
  readonly dragProps: CardDragProps;
}

/**
 * One card — **presentational** (AC-32): the display title (title → posting title → preview), the CV
 * line (label → filename → *"CV deleted"*), *"since {date}"*, a link to `/history/{runId}/cv`
 * (built with `runLink`), the posting URL as an external link (`rel="noopener noreferrer"`,
 * `target="_blank"`, `http(s)` only), then `MoveToControl`, `CardTitleEditor` and **Remove from
 * board** (*"Removing…"* while pending, *"Not removed — try again."* on failure). Every string from a
 * user or the posting is a text node.
 *
 * SKELETON (T26): renders nothing; T28 builds it.
 */
export function BoardCard(_props: BoardCardProps): React.JSX.Element | null {
  return null;
}
