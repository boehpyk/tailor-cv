/* eslint-disable @typescript-eslint/no-unused-vars -- T26 SKELETON: the props are the signature qa's T27 tests compile against; T28 uses them and deletes this line. */
import type { ColumnDropProps } from '../hooks/useCardDrag';
import type { Stage } from '../types';
import type { ReactNode } from 'react';

export interface BoardColumnProps {
  readonly stage: Stage;
  /** The number of cards in the column, shown in its heading (AC-32). */
  readonly count: number;
  /** Whether a dragged card is over this column — the drop indication (AC-35). */
  readonly isDropTarget: boolean;
  /** From `useCardDrag().columnProps(stage)`. */
  readonly dropProps: ColumnDropProps;
  /** The column's cards (`BoardCard`s), already in the server's order. */
  readonly children: ReactNode;
}

/**
 * One column — **presentational**: a `role="region"` labelled by its heading, *"{Stage} ({count})"*,
 * and a drop target (AC-32, AC-35, AC-40).
 *
 * SKELETON (T26): renders nothing; T28 builds it.
 */
export function BoardColumn(_props: BoardColumnProps): React.JSX.Element | null {
  return null;
}
