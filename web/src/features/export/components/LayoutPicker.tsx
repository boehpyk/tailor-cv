/**
 * The PDF layout picker (slice 3.2, plan §7) — presentational: a native radio group over
 * `LAYOUT_TEMPLATES`, no state of its own beyond what the browser keeps for a radio.
 *
 * SKELETON (T23): the fieldset and legend only; the radios land in T25.
 */

import { PDF_LAYOUT_LEGEND } from '../exportCopy';

import type { LayoutTemplate } from '../types';

export interface LayoutPickerProps {
  /** The effective layout: the user's choice, else the derived pre-selection, else Classic. */
  readonly value: LayoutTemplate;
  readonly onChange: (layout: LayoutTemplate) => void;
  /** While the export list loads, so the selection cannot jump under the pointer (AC-32). */
  readonly disabled: boolean;
  /** Rendered as `aria-busy` on the fieldset. */
  readonly busy: boolean;
}

export function LayoutPicker({ disabled, busy }: LayoutPickerProps): React.JSX.Element {
  return (
    <fieldset disabled={disabled} aria-busy={busy}>
      <legend className="text-sm font-medium text-slate-800">{PDF_LAYOUT_LEGEND}</legend>
    </fieldset>
  );
}
