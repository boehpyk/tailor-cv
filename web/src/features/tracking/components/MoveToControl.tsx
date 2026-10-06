/* eslint-disable @typescript-eslint/no-unused-vars -- T26 SKELETON: the props are the signature qa's T27 tests compile against; T28 uses them and deletes this line. */
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
}

/**
 * **Move to** — a native, labelled `<select>` listing the five other stages (plan §0.10, AC-33,
 * AC-40): keyboard, screen reader and touch for free, and the single-pointer alternative to dragging
 * (WCAG 2.2 SC 2.5.7), always visible.
 *
 * SKELETON (T26): renders nothing; T28 builds it.
 */
export function MoveToControl(_props: MoveToControlProps): React.JSX.Element | null {
  return null;
}
