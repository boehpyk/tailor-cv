export interface DeleteHistoryEntryDialogProps {
  readonly onConfirm: () => void;
  /** Escape and Cancel both land here; focus then returns to the row's Delete. */
  readonly onCancel: () => void;
}

/**
 * The history-entry delete confirmation (AC-45): the shared `components/ui/ConfirmDeleteDialog`
 * (`role="alertdialog"`, focus in, trapped, Escape cancels, focus returned) saying
 * `DELETE_ENTRY_MESSAGE` — what goes with the entry, and that it cannot be undone.
 *
 * SKELETON (T29): a distinguishable stub; T31 renders the shared dialog.
 */
export function DeleteHistoryEntryDialog({
  onConfirm,
  onCancel,
}: DeleteHistoryEntryDialogProps): React.JSX.Element {
  return (
    <div>
      DeleteHistoryEntryDialog (skeleton)
      <button type="button" onClick={onCancel}>
        cancel (skeleton)
      </button>
      <button type="button" onClick={onConfirm}>
        confirm (skeleton)
      </button>
    </div>
  );
}
