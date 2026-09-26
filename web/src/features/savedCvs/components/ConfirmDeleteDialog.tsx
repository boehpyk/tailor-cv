import { useEffect, useId, useRef } from 'react';

import { CANCEL_LABEL, CONFIRM_DELETE_LABEL, confirmDeleteMessage } from '../savedCvsCopy';

export interface ConfirmDeleteDialogProps {
  /** The CV's display name — its label, else its filename. User text; rendered as text (AC-47). */
  readonly name: string;
  readonly onConfirm: () => void;
  /** Escape and Cancel both land here; focus then returns to what opened the dialog. */
  readonly onCancel: () => void;
}

/**
 * The delete confirmation (AC-36, AC-48): `role="alertdialog"`, labelled and described by its own
 * text, focus moved into it on open, trapped while open, **Escape cancels**, and focus returned to
 * the control that opened it on close. It says `confirmDeleteMessage(name)` verbatim — what goes,
 * that it cannot be undone, and that working copies already in a workspace are not affected.
 *
 * Presentational: it does not delete anything; the section's `onConfirm` does.
 *
 * **The one effect is focus**, which is the DOM's, not React's: on mount it remembers what had
 * focus and moves focus to **Cancel** (the safe choice for an irreversible action), and on unmount
 * it gives focus back. Not a native `<dialog>`: `showModal()` is not implemented by the test DOM,
 * and the two behaviours it would add — trapping and Escape — are a few lines here.
 */
export function ConfirmDeleteDialog({
  name,
  onConfirm,
  onCancel,
}: ConfirmDeleteDialogProps): React.JSX.Element {
  const titleId = useId();
  const messageId = useId();
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    cancelRef.current?.focus();
    return () => {
      opener?.focus();
    };
  }, []);

  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      onCancel();
      return;
    }
    if (event.key !== 'Tab') {
      return;
    }
    // Two focusable controls: Tab and Shift+Tab each move to the other one, and never out.
    event.preventDefault();
    const next = document.activeElement === cancelRef.current ? confirmRef : cancelRef;
    next.current?.focus();
  }

  return (
    <div
      role="alertdialog"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={messageId}
      onKeyDown={handleKeyDown}
      className="rounded-lg border border-red-200 bg-red-50 p-4"
    >
      <h3 id={titleId} className="mb-2 text-sm font-semibold text-slate-900">
        Delete saved CV
      </h3>
      <p id={messageId} className="mb-3 text-sm text-slate-700">
        {confirmDeleteMessage(name)}
      </p>
      <div className="flex gap-3">
        <button
          ref={cancelRef}
          type="button"
          onClick={onCancel}
          className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-900"
        >
          {CANCEL_LABEL}
        </button>
        <button
          ref={confirmRef}
          type="button"
          onClick={onConfirm}
          className="rounded-md bg-red-700 px-3 py-1.5 text-sm font-medium text-white"
        >
          {CONFIRM_DELETE_LABEL}
        </button>
      </div>
    </div>
  );
}
