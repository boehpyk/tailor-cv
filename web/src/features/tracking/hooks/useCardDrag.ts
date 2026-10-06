import { useCallback, useRef, useState } from 'react';

import type { BoardCard, Stage } from '../types';

/** Spread onto a card's root element: makes it draggable and records which card is in flight. */
export interface CardDragProps {
  readonly draggable: boolean;
  readonly onDragStart: (event: React.DragEvent<HTMLElement>) => void;
  readonly onDragEnd: (event: React.DragEvent<HTMLElement>) => void;
}

/** Spread onto a column's region: makes it a drop target. */
export interface ColumnDropProps {
  readonly onDragOver: (event: React.DragEvent<HTMLElement>) => void;
  readonly onDragEnter: (event: React.DragEvent<HTMLElement>) => void;
  readonly onDragLeave: (event: React.DragEvent<HTMLElement>) => void;
  readonly onDrop: (event: React.DragEvent<HTMLElement>) => void;
}

export interface CardDrag {
  /**
   * The props for one card. Its stage is how a drop on its own column is told apart (AC-35). The
   * same object for the same id and stage, so a memoized card is not re-rendered by a new one.
   */
  readonly cardProps: (card: Pick<BoardCard, 'id' | 'stage'>) => CardDragProps;
  /** The props for one column. */
  readonly columnProps: (stage: Stage) => ColumnDropProps;
  /** The column under the pointer while a card is dragged — the drop indication (AC-35). */
  readonly overStage: Stage | null;
}

type Dragged = Pick<BoardCard, 'id' | 'stage'>;

/**
 * Native HTML5 drag-and-drop for pointer users (plan §0.10, AC-35). The dragged card lives in a
 * **ref** — it changes no render until the drop — and a drop on another column calls `onMove(cardId,
 * stage)`, which the page wires to the **same** `useMoveTrackedApplication` mutation as the Move
 * control. A drop on the card's own column calls nothing. Touch never fires these events (T-40); the
 * Move control is the path there, always visible.
 *
 * `overStage` *is* state, because it changes what renders (the drop indication).
 *
 * The dragged card is read from the ref, not from `dataTransfer`: a page only ever drops its own
 * cards, and `dataTransfer` is readable only on `drop` (never on `dragover`), so it could not decide
 * whether a column accepts. The id is still put in `dataTransfer`, because Firefox starts no drag
 * without data. A drag from elsewhere (a file, a link) finds an empty ref and is ignored: no
 * `preventDefault`, so the browser shows "no drop" there.
 */
export function useCardDrag(onMove: (cardId: string, stage: Stage) => void): CardDrag {
  const dragged = useRef<Dragged | null>(null);
  const [overStage, setOverStage] = useState<Stage | null>(null);

  // One props object per (card, stage), kept: `BoardCard` is memoized, and a fresh object on every
  // render would re-render all 500 cards on a move (AC-44). The handlers read only the id and the
  // stage, so an entry never goes stale; at most six entries per card.
  const cardPropsCache = useRef(new Map<string, CardDragProps>());
  const cardProps = useCallback((card: Dragged): CardDragProps => {
    const { id, stage } = card;
    const cacheKey = `${stage}:${id}`; // a stage never contains ":"
    const cached = cardPropsCache.current.get(cacheKey);
    if (cached !== undefined) {
      return cached;
    }
    const props: CardDragProps = {
      draggable: true,
      onDragStart: (event) => {
        dragged.current = { id, stage };
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', id);
      },
      onDragEnd: () => {
        dragged.current = null;
        setOverStage(null);
      },
    };
    cardPropsCache.current.set(cacheKey, props);
    return props;
  }, []);

  const columnProps = useCallback(
    (stage: Stage): ColumnDropProps => ({
      onDragOver: (event) => {
        if (dragged.current === null) {
          return;
        }
        // Without this the browser refuses the drop: `dragover`'s default is "not a target".
        event.preventDefault();
        event.dataTransfer.dropEffect = 'move';
      },
      onDragEnter: (event) => {
        if (dragged.current === null) {
          return;
        }
        event.preventDefault();
        setOverStage(stage);
      },
      onDragLeave: (event) => {
        // Moving onto a card inside the column fires `dragleave` on the column; that is not leaving.
        const next = event.relatedTarget;
        if (next instanceof Node && event.currentTarget.contains(next)) {
          return;
        }
        setOverStage((current) => (current === stage ? null : current));
      },
      onDrop: (event) => {
        const card = dragged.current;
        dragged.current = null;
        setOverStage(null);
        if (card === null) {
          return;
        }
        event.preventDefault();
        if (card.stage !== stage) {
          onMove(card.id, stage);
        }
      },
    }),
    [onMove],
  );

  return { cardProps, columnProps, overStage };
}
