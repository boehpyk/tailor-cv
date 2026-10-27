import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useNavigate } from 'react-router';

import { ApiError } from '@/api/client';
import { ConfirmDeleteDialog } from '@/components/ui/ConfirmDeleteDialog';

import {
  CANCEL_LABEL,
  DELETE_ALREADY_GONE,
  DELETE_CV_DIALOG_TITLE,
  DELETE_CV_LABEL,
  DELETE_ENTRY_DIALOG_TITLE,
  DELETE_ENTRY_LABEL,
  DELETE_FAILED,
  DELETE_IN_PROGRESS,
  DELETE_PENDING_LABEL,
  USER_GONE,
  deleteCvDialogMessage,
  deleteEntryDialogMessage,
  deletedNotice,
} from '../adminCopy';
import { contentNoticeState } from '../contentNotice';
import { deleteContentMutationKey, useDeleteContent } from '../hooks/useDeleteContent';

import type { ContentNotice } from '../contentNotice';
import type { ContentDeletion } from '../hooks/useDeleteContent';

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
      /** The entry's posting title (or the untitled/unavailable wording) — also its name. */
      readonly postingTitle: string;
      /** ISO-8601 `requested_at`; the dialog shows its date. */
      readonly requestedAt: string;
    };

export interface DeleteContentButtonProps {
  readonly actorId: string;
  readonly userId: string;
  readonly target: DeleteContentTarget;
}

function nameOf(target: DeleteContentTarget): string {
  return target.kind === 'cv' ? target.name : target.postingTitle;
}

function noticeFor(target: DeleteContentTarget, deletion: ContentDeletion): ContentNotice {
  if (deletion.outcome === 'already_gone') return { text: DELETE_ALREADY_GONE, unlinkFailures: 0 };
  const unlinkFailures =
    deletion.kind === 'cv'
      ? deletion.report.file_unlinked
        ? 0
        : 1
      : deletion.report.unlink_failures;
  return { text: deletedNotice(nameOf(target)), unlinkFailures };
}

/** A refusal, in words. Nothing changed; the button is usable again (writes hold, ADR-0031). */
function failureCopy(error: Error): string {
  if (error instanceof ApiError && error.code === 'tailoring_run_in_progress') {
    return DELETE_IN_PROGRESS;
  }
  if (error instanceof ApiError && error.code === 'user_not_found') return USER_GONE;
  return DELETE_FAILED;
}

/**
 * *Delete CV* / *Delete history entry* (AC-29): `ConfirmDeleteDialog` naming the item and what
 * goes with it, then `useDeleteContent`. Success — or the kind's 404, *Already deleted.* — goes
 * back to the account with the notice in `location.state`; a 409 (still running) or any other
 * refusal is an alert here with the dialog closed. Not optimistic; a same-tick double submit is
 * stopped by `isMutating`, which `mutate()` updates synchronously (2.1).
 */
export function DeleteContentButton({
  actorId,
  userId,
  target,
}: DeleteContentButtonProps): React.JSX.Element {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [confirming, setConfirming] = useState(false);
  const remove = useDeleteContent(actorId, userId, target.kind, target.id, {
    onDeleted: (deletion) =>
      navigate(`/admin/users/${encodeURIComponent(userId)}`, {
        state: contentNoticeState(noticeFor(target, deletion)),
      }),
  });
  const label = target.kind === 'cv' ? DELETE_CV_LABEL : DELETE_ENTRY_LABEL;

  function confirm(): void {
    setConfirming(false);
    if (queryClient.isMutating({ mutationKey: deleteContentMutationKey }) > 0) return;
    remove.mutate();
  }

  return (
    <div className="space-y-2">
      <button
        type="button"
        disabled={remove.isPending}
        onClick={() => {
          setConfirming(true);
        }}
        className="rounded-md border border-danger-200 bg-white px-4 py-2 text-sm font-semibold text-danger-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 disabled:opacity-60"
      >
        {remove.isPending ? DELETE_PENDING_LABEL : label}
      </button>
      {remove.isError && (
        <p role="alert" className="text-sm text-danger-600">
          {failureCopy(remove.error)}
        </p>
      )}
      {confirming && (
        <ConfirmDeleteDialog
          title={target.kind === 'cv' ? DELETE_CV_DIALOG_TITLE : DELETE_ENTRY_DIALOG_TITLE}
          message={
            target.kind === 'cv'
              ? deleteCvDialogMessage(target.name)
              : deleteEntryDialogMessage(
                  target.postingTitle,
                  new Date(target.requestedAt).toLocaleDateString(),
                )
          }
          confirmLabel={label}
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
