import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { ConfirmDeleteDialog } from '@/components/ui/ConfirmDeleteDialog';

import {
  CANCEL_LABEL,
  DELETE_GUEST_DIALOG_TITLE,
  DELETE_GUEST_FAILED,
  DELETE_GUEST_LABEL,
  DELETE_PENDING_LABEL,
  deleteGuestDialogMessage,
} from '../adminCopy';
import { eraseGuestSessionMutationKey, useEraseGuestSession } from '../hooks/useEraseGuestSession';

import type { AdminGuestSessionSummary } from '@/api/admin';

export interface DeleteGuestSessionButtonProps {
  readonly actorId: string;
  /** The session and its counts, which the dialog names (AC-34). */
  readonly session: AdminGuestSessionSummary;
  /** Any run `queued`/`running`: the dialog adds *a run still in progress is discarded*. */
  readonly runInProgress: boolean;
}

/**
 * *Delete guest session* (AC-34): `ConfirmDeleteDialog` naming what goes, then
 * `useEraseGuestSession`, which navigates on success and refetches on a 404. Any other refusal is
 * an alert here and the button is usable again (writes hold, ADR-0031). Not optimistic; a
 * same-tick double submit is stopped by `isMutating` (2.1).
 */
export function DeleteGuestSessionButton({
  actorId,
  session,
  runInProgress,
}: DeleteGuestSessionButtonProps): React.JSX.Element {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const erase = useEraseGuestSession(actorId, session.id);

  function confirm(): void {
    setConfirming(false);
    if (queryClient.isMutating({ mutationKey: eraseGuestSessionMutationKey }) > 0) return;
    erase.mutate();
  }

  return (
    <div className="space-y-2">
      <button
        type="button"
        disabled={erase.isPending}
        onClick={() => {
          setConfirming(true);
        }}
        className="rounded-md border border-danger-200 bg-white px-4 py-2 text-sm font-semibold text-danger-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 disabled:opacity-60"
      >
        {erase.isPending ? DELETE_PENDING_LABEL : DELETE_GUEST_LABEL}
      </button>
      {erase.isError && (
        <p role="alert" className="text-sm text-danger-600">
          {DELETE_GUEST_FAILED}
        </p>
      )}
      {confirming && (
        <ConfirmDeleteDialog
          title={DELETE_GUEST_DIALOG_TITLE}
          message={deleteGuestDialogMessage(session, runInProgress)}
          confirmLabel={DELETE_GUEST_LABEL}
          cancelLabel={CANCEL_LABEL}
          onConfirm={confirm}
          onCancel={() => {
            setConfirming(false);
          }}
        />
      )}
    </div>
  );
}
