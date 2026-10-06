/* eslint-disable @typescript-eslint/no-unused-vars -- T26 SKELETON: the parameters are the signatures qa's T27 tests compile against; T28 uses them and deletes this line. */
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
  /** The props for one card. Its stage is how a drop on its own column is told apart (AC-35). */
  readonly cardProps: (card: Pick<BoardCard, 'id' | 'stage'>) => CardDragProps;
  /** The props for one column. */
  readonly columnProps: (stage: Stage) => ColumnDropProps;
  /** The column under the pointer while a card is dragged — the drop indication (AC-35). */
  readonly overStage: Stage | null;
}

/**
 * Native HTML5 drag-and-drop for pointer users (plan §0.10, AC-35). The dragged card lives in a
 * **ref** — it changes no render until the drop — and a drop on another column calls `onMove(cardId,
 * stage)`, which the page wires to the **same** `useMoveTrackedApplication` mutation as the Move
 * control. A drop on the card's own column calls nothing. Touch never fires these events (T-40); the
 * Move control is the path there, always visible.
 *
 * SKELETON (T26): inert props, no drag state, `onMove` never called.
 */
export function useCardDrag(_onMove: (cardId: string, stage: Stage) => void): CardDrag {
  const noop = (): void => undefined;
  return {
    cardProps: () => ({ draggable: false, onDragStart: noop, onDragEnd: noop }),
    columnProps: () => ({ onDragOver: noop, onDragEnter: noop, onDragLeave: noop, onDrop: noop }),
    overStage: null,
  };
}
