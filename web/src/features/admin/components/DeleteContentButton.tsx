/** What is being deleted, and the words the dialog names it by (AC-29). */
export type DeleteContentTarget =
  | {
      readonly kind: 'cv';
      readonly id: string;
      /** The CV's label, else its filename. */
      readonly name: string;
    }
  | {
      readonly kind: 'history';
      readonly id: string;
      /** The entry's posting title (or the untitled/unavailable wording). */
      readonly postingTitle: string;
      /** ISO-8601 `requested_at`; the dialog shows its date. */
      readonly requestedAt: string;
    };

export interface DeleteContentButtonProps {
  readonly actorId: string;
  readonly userId: string;
  readonly target: DeleteContentTarget;
}

/**
 * *Delete CV* / *Delete history entry* (AC-29): `ConfirmDeleteDialog` + `useDeleteContent`.
 * Success → back to the account with the notice; 409 → *This run is still working…*; the kind's
 * 404 → *Already deleted.*; double submit guarded with `isMutating`. Not optimistic.
 *
 * SKELETON (T11): a distinguishable placeholder.
 */
export function DeleteContentButton({
  actorId,
  userId,
  target,
}: DeleteContentButtonProps): React.JSX.Element {
  return (
    <div data-actor={actorId} data-user={userId} data-kind={target.kind} data-item={target.id} />
  );
}
