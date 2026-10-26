import { useId, useRef, useState } from 'react';

import { EXPORT_SHEET_CLOSE_LABEL, EXPORT_SHEET_TOGGLE_LABEL } from '../exportCopy';

import type { KeyboardEvent, ReactNode } from 'react';

export interface ExportSheetProps {
  /** The run's one `ExportBar` — rendered exactly once, open or closed (AC-23). */
  readonly children: ReactNode;
}

const FOCUS_RING =
  'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600';

/**
 * The export bar's mobile presentation (web-design-refresh AC-23, OQ-10): a disclosure, not a
 * `<dialog>`. Below `sm` a *Download* toggle opens the bar as a bottom sheet; at `sm`+ the bar is an
 * ordinary inline section and the toggle is hidden. One instance of the bar either way, so no
 * reader ever meets two.
 *
 * Every presentation class is `max-sm:`-prefixed, so at `sm`+ nothing here applies. While closed
 * below `sm` the sheet is `invisible` (visibility: hidden), which takes it out of the tab order and
 * the accessibility tree — not `hidden`/`inert`, which would also apply at `sm`+ (R-2).
 *
 * `open` is component state: it dies with the component, and no storage or URL ever sees it. Escape
 * is an `onKeyDown` on the sheet, so it only fires while focus is inside — no document listener and
 * no effect.
 */
export function ExportSheet({ children }: ExportSheetProps): React.JSX.Element {
  const id = useId();
  const [open, setOpen] = useState(false);
  const toggleRef = useRef<HTMLButtonElement>(null);

  function close(): void {
    setOpen(false);
    toggleRef.current?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'Escape') {
      event.stopPropagation();
      close();
    }
  }

  return (
    <>
      <button
        ref={toggleRef}
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => {
          setOpen((wasOpen) => !wasOpen);
        }}
        className={`min-h-11 rounded-full border border-slate-300 bg-white px-4 text-sm font-semibold text-slate-900 shadow-sm hover:bg-surface-sunk sm:hidden ${FOCUS_RING}`}
      >
        {EXPORT_SHEET_TOGGLE_LABEL}
      </button>
      <div
        id={id}
        onKeyDown={onKeyDown}
        className={`max-sm:fixed max-sm:inset-x-0 max-sm:bottom-0 max-sm:z-40 max-sm:max-h-[85dvh] max-sm:overflow-y-auto max-sm:rounded-t-2xl max-sm:bg-white max-sm:p-4 max-sm:shadow-2xl motion-safe:transition-transform ${
          open ? '' : 'max-sm:invisible max-sm:translate-y-full'
        }`}
      >
        <div className="flex justify-end sm:hidden">
          <button
            type="button"
            onClick={close}
            className={`min-h-11 rounded-full px-4 text-sm font-semibold text-slate-700 hover:bg-surface-sunk ${FOCUS_RING}`}
          >
            {EXPORT_SHEET_CLOSE_LABEL}
          </button>
        </div>
        {children}
      </div>
    </>
  );
}
