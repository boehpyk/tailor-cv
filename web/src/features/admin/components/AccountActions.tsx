import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useNavigate } from 'react-router';

import { ApiError } from '@/api/client';
import { ConfirmDeleteDialog } from '@/components/ui/ConfirmDeleteDialog';

import {
  ACTION_FAILED,
  CANCEL_LABEL,
  DISABLE_DIALOG_TITLE,
  DISABLE_LABEL,
  DISABLE_PENDING_LABEL,
  ENABLE_LABEL,
  ENABLE_PENDING_LABEL,
  ERASE_DIALOG_TITLE,
  ERASE_LABEL,
  ERASE_PENDING_LABEL,
  USER_GONE,
  disableDialogMessage,
  eraseDialogMessage,
} from '../adminCopy';
import { erasedNoticeState } from '../erasedNotice';
import { accountActionMutationKey, useAccountAction } from '../hooks/useAccountAction';

import type { AdminUserDetail } from '@/api/admin';
import type { AccountAction } from '../hooks/useAccountAction';

export interface AccountActionsProps {
  /** The signed-in admin; acting on their own account signs the tab out (OQ-18). */
  readonly actorId: string;
  readonly user: AdminUserDetail;
}

/** A refusal, in words: a 404 means the account is gone; anything else, this action failed. */
function failureCopy(action: AccountAction, error: Error): string {
  return error instanceof ApiError && error.code === 'user_not_found'
    ? USER_GONE
    : ACTION_FAILED[action];
}

/**
 * Disable (confirmed) / Enable (one click) / Erase (confirmed) for one account (AC-34, AC-35,
 * OQ-12). Not optimistic: the status changes when the server says so. A refusal is shown in place
 * and never re-sent (writes hold, ADR-0031). A same-tick double click is stopped by
 * `isMutating`, which `mutate()` updates synchronously (2.1).
 */
export function AccountActions({ actorId, user }: AccountActionsProps): React.JSX.Element {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [confirming, setConfirming] = useState<'disable' | 'erase' | null>(null);
  const ownAccount = actorId === user.id;
  const action = useAccountAction(actorId, user.id, {
    onErased: (report) =>
      navigate('/admin', {
        state: erasedNoticeState({ email: user.email, unlinkFailures: report.unlink_failures }),
      }),
  });
  const pending = action.isPending ? action.variables : undefined;

  function run(next: AccountAction): void {
    setConfirming(null);
    if (queryClient.isMutating({ mutationKey: accountActionMutationKey }) > 0) return;
    action.mutate(next);
  }

  const toggle: AccountAction = user.disabled_at === null ? 'disable' : 'enable';
  const toggleLabel =
    pending === 'disable'
      ? DISABLE_PENDING_LABEL
      : pending === 'enable'
        ? ENABLE_PENDING_LABEL
        : toggle === 'disable'
          ? DISABLE_LABEL
          : ENABLE_LABEL;

  return (
    <div className="mt-6 space-y-3">
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={action.isPending}
          onClick={() => {
            if (toggle === 'enable') run('enable');
            else setConfirming('disable');
          }}
          className="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-900 disabled:opacity-60"
        >
          {toggleLabel}
        </button>
        <button
          type="button"
          disabled={action.isPending}
          onClick={() => {
            setConfirming('erase');
          }}
          className="rounded-md border border-red-300 px-4 py-2 text-sm font-medium text-red-700 disabled:opacity-60"
        >
          {pending === 'erase' ? ERASE_PENDING_LABEL : ERASE_LABEL}
        </button>
      </div>
      {action.isError && (
        <p role="alert" className="text-sm text-red-700">
          {failureCopy(action.variables, action.error)}
        </p>
      )}
      {confirming === 'disable' && (
        <ConfirmDeleteDialog
          title={DISABLE_DIALOG_TITLE}
          message={disableDialogMessage(user.email, ownAccount)}
          confirmLabel={DISABLE_LABEL}
          cancelLabel={CANCEL_LABEL}
          onConfirm={() => {
            run('disable');
          }}
          onCancel={() => {
            setConfirming(null);
          }}
        />
      )}
      {confirming === 'erase' && (
        <ConfirmDeleteDialog
          title={ERASE_DIALOG_TITLE}
          message={eraseDialogMessage(user.email, user.counts, ownAccount)}
          confirmLabel={ERASE_LABEL}
          cancelLabel={CANCEL_LABEL}
          onConfirm={() => {
            run('erase');
          }}
          onCancel={() => {
            setConfirming(null);
          }}
        />
      )}
    </div>
  );
}
