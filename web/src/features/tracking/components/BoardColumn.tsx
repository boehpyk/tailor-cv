import { useId } from 'react';

import { columnHeading } from '../trackingCopy';

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
 * and a drop target (AC-32, AC-35, AC-40). The region is the drop target rather than the list, so a
 * drop on an empty column's heading still lands.
 */
export function BoardColumn({
  stage,
  count,
  isDropTarget,
  dropProps,
  children,
}: BoardColumnProps): React.JSX.Element {
  const headingId = useId();
  return (
    <section
      role="region"
      aria-labelledby={headingId}
      data-stage={stage}
      {...dropProps}
      className={`min-h-24 rounded-lg border p-3 lg:w-60 lg:shrink-0 ${
        isDropTarget ? 'border-slate-900 bg-slate-100' : 'border-slate-200 bg-slate-50'
      }`}
    >
      <h3 id={headingId} className="mb-2 text-sm font-semibold text-slate-700">
        {columnHeading(stage, count)}
      </h3>
      <ul className="space-y-2">{children}</ul>
    </section>
  );
}
