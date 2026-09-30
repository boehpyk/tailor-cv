import { ConfirmDeleteDialog } from '@/components/ui/ConfirmDeleteDialog';

import {
  DELETE_ENTRY_CANCEL_LABEL,
  DELETE_ENTRY_CONFIRM_LABEL,
  DELETE_ENTRY_DIALOG_TITLE,
  DELETE_ENTRY_MESSAGE,
} from '../historyCopy';

export interface DeleteHistoryEntryDialogProps {
  readonly onConfirm: () => void;
  /** Escape and Cancel both land here; focus then returns to the row's Delete. */
  readonly onCancel: () => void;
}

/**
 * The history-entry delete confirmation (AC-45): the shared `components/ui/ConfirmDeleteDialog`
 * (`role="alertdialog"`, focus in on Cancel, trapped, Escape cancels, focus returned to the
 * control that opened it) saying `DELETE_ENTRY_MESSAGE` — what goes with the entry, and that it
 * cannot be undone. Presentational: the page's `onConfirm` deletes.
 */
export function DeleteHistoryEntryDialog({
  onConfirm,
  onCancel,
}: DeleteHistoryEntryDialogProps): React.JSX.Element {
  return (
    <ConfirmDeleteDialog
      title={DELETE_ENTRY_DIALOG_TITLE}
      message={DELETE_ENTRY_MESSAGE}
      confirmLabel={DELETE_ENTRY_CONFIRM_LABEL}
      cancelLabel={DELETE_ENTRY_CANCEL_LABEL}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  );
}
