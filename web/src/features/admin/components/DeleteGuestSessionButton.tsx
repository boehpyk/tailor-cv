import type { AdminGuestSessionSummary } from '@/api/admin';

export interface DeleteGuestSessionButtonProps {
  readonly actorId: string;
  /** The session and its counts, which the dialog names (AC-34). */
  readonly session: AdminGuestSessionSummary;
  /** Any run `queued`/`running`: the dialog adds *a run still in progress is discarded*. */
  readonly runInProgress: boolean;
}

/**
 * *Delete guest session* (AC-34): `ConfirmDeleteDialog` + `useEraseGuestSession`. Not optimistic;
 * `isMutating` guard.
 *
 * SKELETON (T20): an empty, data-attributed placeholder; T22 builds it.
 */
export function DeleteGuestSessionButton({
  actorId,
  session,
  runInProgress,
}: DeleteGuestSessionButtonProps): React.JSX.Element {
  return (
    <div
      data-admin-stub="delete-guest-session"
      data-actor={actorId}
      data-session={session.id}
      data-run-in-progress={String(runInProgress)}
    />
  );
}
