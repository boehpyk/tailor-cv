import { useId } from 'react';

import { EXPORT_SHEET_CLOSE_LABEL, EXPORT_SHEET_TOGGLE_LABEL } from '../exportCopy';

import type { ReactNode } from 'react';

export interface ExportSheetProps {
  /** The run's one `ExportBar` — rendered exactly once, open or closed (AC-23). */
  readonly children: ReactNode;
}

/**
 * The export bar's mobile presentation (web-design-refresh AC-23, OQ-10): a disclosure, not a
 * `<dialog>`. Below `sm` a *Download* toggle opens the bar as a bottom sheet; at `sm`+ the bar is an
 * ordinary inline section and the toggle is hidden. One instance of the bar either way, so no
 * reader ever meets two.
 *
 * SKELETON (T23): the final roles, ids and copy, with `open` fixed `false` and no behaviour — no
 * toggle, no Escape, no focus return. T25 adds them.
 */
export function ExportSheet({ children }: ExportSheetProps): React.JSX.Element {
  const id = useId();
  const open = false;
  return (
    <>
      <button type="button" aria-expanded={open} aria-controls={id} className="sm:hidden">
        {EXPORT_SHEET_TOGGLE_LABEL}
      </button>
      <div id={id}>
        <button type="button" className="sm:hidden">
          {EXPORT_SHEET_CLOSE_LABEL}
        </button>
        {children}
      </div>
    </>
  );
}
