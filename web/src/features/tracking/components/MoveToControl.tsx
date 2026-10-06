import { MOVE_PENDING_LABEL, MOVE_TO_LABEL, STAGE_LABELS } from '../trackingCopy';
import { STAGES, isStage } from '../types';

import type { Stage } from '../types';

export interface MoveToControlProps {
  /** The card's current stage — the one stage the control does **not** offer. */
  readonly stage: Stage;
  /** A move is on the wire: disabled, reading *"Saving…"* (T-39). */
  readonly pending: boolean;
  /** Called with the chosen stage, once per choice. */
  readonly onMove: (stage: Stage) => void;
  /** React 19's ref-as-prop, onto the `<select>` — where focus lands after a move (AC-33). */
  readonly ref?: React.Ref<HTMLSelectElement>;
  /** Extra context for assistive technology — the card's title, so "Move to" says *which* card. */
  readonly describedBy?: string;
}

/**
 * **Move to** — a native, labelled `<select>` listing the five other stages (plan §0.10, AC-33,
 * AC-40): keyboard, screen reader and touch for free, and the single-pointer alternative to dragging
 * (WCAG 2.2 SC 2.5.7), always visible.
 *
 * **A command, not a field.** The select is controlled at `""` — a disabled placeholder option
 * (*"Move to…"*, or *"Saving…"* while pending) — so it never displays a stage as if it were the
 * card's value, every choice is a fresh `change` (choosing the same stage twice still fires), and
 * the card's stage is shown by the column it sits in, the one source of truth.
 */
export function MoveToControl({
  stage,
  pending,
  onMove,
  ref,
  describedBy,
}: MoveToControlProps): React.JSX.Element {
  return (
    <select
      ref={ref}
      aria-label={MOVE_TO_LABEL}
      aria-describedby={describedBy}
      value=""
      disabled={pending}
      onChange={(event) => {
        const chosen = event.target.value;
        if (isStage(chosen)) {
          onMove(chosen);
        }
      }}
      className="rounded-md border border-slate-300 bg-white px-2 py-1 text-sm text-slate-800 disabled:opacity-60"
    >
      <option value="" disabled>
        {pending ? MOVE_PENDING_LABEL : `${MOVE_TO_LABEL}…`}
      </option>
      {STAGES.filter((other) => other !== stage).map((other) => (
        <option key={other} value={other}>
          {STAGE_LABELS[other]}
        </option>
      ))}
    </select>
  );
}
