import { DISABLE_LABEL, ENABLE_LABEL, ERASE_LABEL } from '../adminCopy';

import type { AdminUserDetail } from '@/api/admin';

export interface AccountActionsProps {
  /** The signed-in admin; acting on their own account signs the tab out (OQ-18). */
  readonly actorId: string;
  readonly user: AdminUserDetail;
}

/**
 * Disable (confirmed) / Enable (not confirmed) / Erase (confirmed), each through
 * `useAccountAction`, with `ConfirmDeleteDialog` and the refusal in place (AC-34, AC-35).
 *
 * SKELETON (T19): the two buttons for the account's state, inert; behaviour comes in T21.
 */
export function AccountActions({ actorId, user }: AccountActionsProps): React.JSX.Element {
  return (
    <div className="mt-6 flex gap-2" data-actor={actorId}>
      <button type="button" className="rounded-md border border-slate-300 px-4 py-2 text-sm">
        {user.disabled_at === null ? DISABLE_LABEL : ENABLE_LABEL}
      </button>
      <button type="button" className="rounded-md border border-red-300 px-4 py-2 text-sm">
        {ERASE_LABEL}
      </button>
    </div>
  );
}
