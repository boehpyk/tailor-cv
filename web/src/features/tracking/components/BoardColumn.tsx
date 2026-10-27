import { useId } from 'react';

import { STAGE_LABELS, columnHeading } from '../trackingCopy';

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
      // The drop indication as a fact, not only a colour: a test (and a stylesheet) can read it.
      data-drop-target={isDropTarget ? 'true' : 'false'}
      {...dropProps}
      className={`min-h-24 rounded-2xl p-3 max-sm:px-0 lg:w-64 lg:shrink-0 ${
        isDropTarget
          ? 'bg-brand-50 ring-2 ring-brand-600'
          : 'max-sm:bg-transparent sm:bg-surface-sunk'
      }`}
    >
      {/* The name is the copy's own sentence, *"{Stage} ({count})"*; what shows is the label and
          the count as a pill — the same words, so the visible label is inside the name. */}
      <h3
        id={headingId}
        aria-label={columnHeading(stage, count)}
        className="mb-3 flex items-center justify-between gap-2 px-1 text-xs font-semibold tracking-wide text-slate-700 uppercase"
      >
        <span aria-hidden="true">{STAGE_LABELS[stage]}</span>
        <span
          aria-hidden="true"
          className="min-w-6 rounded-full border border-slate-200 bg-white px-2 py-0.5 text-center text-xs font-semibold text-slate-700"
        >
          {count}
        </span>
      </h3>
      <ul className="space-y-2.5">{children}</ul>
    </section>
  );
}
