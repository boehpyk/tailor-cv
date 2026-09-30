import { useEffect, useId, useRef } from 'react';

export interface ConfirmDeleteDialogProps {
  /** The dialog's heading — what is being deleted, in a few words. */
  readonly title: string;
  /**
   * What goes, and that it cannot be undone. Rendered as text — callers that fold user text into it
   * (a CV's label) rely on that (2.2's AC-47).
   */
  readonly message: string;
  readonly confirmLabel: string;
  readonly cancelLabel: string;
  readonly onConfirm: () => void;
  /** Escape and Cancel both land here; focus then returns to what opened the dialog. */
  readonly onCancel: () => void;
}

/**
 * A confirmation for an **irreversible** delete — moved here from `features/savedCvs/` in slice 2.3,
 * because a history entry's deletion needs the same dialog (AC-45) and two copies of a focus trap
 * are two places for it to break. Shared, presentational, no data fetching: every word comes in as
 * a prop, and the caller's `onConfirm` does the deleting.
 *
 * `role="alertdialog"`, labelled and described by its own text, focus moved into it on open,
 * trapped while open, **Escape cancels**, and focus returned to the control that opened it on close
 * (2.2's AC-36/AC-48, 2.3's AC-45/AC-50).
 *
 * **The one effect is focus**, which is the DOM's, not React's: on mount it remembers what had
 * focus and moves focus to **Cancel** (the safe choice for an irreversible action), and on unmount
 * it gives focus back. Not a native `<dialog>`: `showModal()` is not implemented by the test DOM,
 * and the two behaviours it would add — trapping and Escape — are a few lines here.
 */
export function ConfirmDeleteDialog({
  title,
  message,
  confirmLabel,
  cancelLabel,
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
        {title}
      </h3>
      <p id={messageId} className="mb-3 text-sm text-slate-700">
        {message}
      </p>
      <div className="flex gap-3">
        <button
          ref={cancelRef}
          type="button"
          onClick={onCancel}
          className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-900"
        >
          {cancelLabel}
        </button>
        <button
          ref={confirmRef}
          type="button"
          onClick={onConfirm}
          className="rounded-md bg-red-700 px-3 py-1.5 text-sm font-medium text-white"
        >
          {confirmLabel}
        </button>
      </div>
    </div>
  );
}
