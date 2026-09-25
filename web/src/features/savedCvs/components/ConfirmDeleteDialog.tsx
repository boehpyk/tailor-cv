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
 * SKELETON (T25): renders nothing. GREEN is T27.
 */
// Typed as a function value, not declared with a parameter, only while it is a stub: the props are
// real and checked at every call site, and there is no unused parameter for the linter to refuse.
export const ConfirmDeleteDialog: (
  props: ConfirmDeleteDialogProps,
) => React.JSX.Element | null = () => null;
