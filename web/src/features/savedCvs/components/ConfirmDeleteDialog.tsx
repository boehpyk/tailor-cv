import { ConfirmDeleteDialog as ConfirmDialog } from '@/components/ui/ConfirmDeleteDialog';

import { CANCEL_LABEL, CONFIRM_DELETE_LABEL, confirmDeleteMessage } from '../savedCvsCopy';

export interface ConfirmDeleteDialogProps {
  /** The CV's display name — its label, else its filename. User text; rendered as text (AC-47). */
  readonly name: string;
  readonly onConfirm: () => void;
  /** Escape and Cancel both land here; focus then returns to what opened the dialog. */
  readonly onCancel: () => void;
}

/**
 * The saved-CV delete confirmation (2.2's AC-36, AC-48): the shared dialog
 * (`components/ui/ConfirmDeleteDialog`, moved there in slice 2.3) with this feature's words. It
 * says `confirmDeleteMessage(name)` verbatim — what goes, that it cannot be undone, and that working
 * copies already in a workspace are not affected.
 *
 * Presentational: it does not delete anything; the section's `onConfirm` does. The focus trap,
 * Escape and focus return live in the shared component, once.
 */
export function ConfirmDeleteDialog({
  name,
  onConfirm,
  onCancel,
}: ConfirmDeleteDialogProps): React.JSX.Element {
  return (
    <ConfirmDialog
      title="Delete saved CV"
      message={confirmDeleteMessage(name)}
      confirmLabel={CONFIRM_DELETE_LABEL}
      cancelLabel={CANCEL_LABEL}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  );
}
