import { useEffect, useId, useRef } from 'react';
import { useMutationState, useQueryClient } from '@tanstack/react-query';

import { useCurrentUser } from '@/features/auth/hooks/useCurrentUser';

import {
  CLOSE,
  OPT_IN_ERROR,
  OPT_IN_SAVING,
  PACK_CLICK_ERROR,
  PACK_DIALOG_HEADING,
  PACK_OPENING_SOON,
  PACK_TRY_AGAIN,
  optInLabel,
} from '../billingCopy';
import { useCredits } from '../hooks/useCredits';
import { packClickMutationKey, usePackClick } from '../hooks/usePackClick';
import { paymentNoticeMutationKey, usePaymentNotice } from '../hooks/usePaymentNotice';

import type { PackTerms } from '../types';

export interface PackDialogProps {
  /** The signed-in user: roots the pack click's and the opt-in's keys and the credits read. */
  readonly userId: string;
  /** The pack that was clicked. */
  readonly pack: PackTerms;
  /** Closes the dialog; the opener owns whether it is open. */
  readonly onClose: () => void;
}

/**
 * Opened by a pack click (AC-35). The click's outcome is read from the mutation cache, filtered to
 * this pack (the opener sent it): pending shows nothing here (it is on the button), error says
 * *That didn't go through. Nothing was charged.* with *Try again*, success says payments are
 * opening soon and offers the opt-in naming the account's address, its checked state the credits
 * read's (never a copy), with its own pending and error.
 *
 * Non-modal and in flow, like `ConfirmDeleteDialog`: focus moves in on open and back on close, and
 * Escape closes. The outcome lands in a live region that was mounted empty.
 */
export function PackDialog({ userId, pack, onClose }: PackDialogProps): React.JSX.Element {
  const headingId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();
  const retry = usePackClick(userId);
  const optIn = usePaymentNotice(userId);
  const optedIn = useCredits(userId).data?.payment_notice_opted_in ?? false;
  const email = useCurrentUser().data?.email;
  const clickKey = packClickMutationKey(userId);
  const optInKey = paymentNoticeMutationKey(userId);
  const statuses = useMutationState({
    filters: { mutationKey: clickKey, predicate: (m) => m.state.variables === pack.id },
    select: (m) => m.state.status,
  });
  const status = statuses.at(-1) ?? 'idle';

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialogRef.current?.focus();
    return () => {
      opener?.focus();
    };
  }, []);

  function tryAgain(): void {
    const inFlight = queryClient.isMutating({
      mutationKey: clickKey,
      predicate: (m) => m.state.variables === pack.id,
    });
    if (inFlight === 0) {
      retry.mutate(pack.id);
    }
  }

  function toggle(next: boolean): void {
    if (queryClient.isMutating({ mutationKey: optInKey }) === 0) {
      optIn.mutate(next);
    }
  }

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-labelledby={headingId}
      tabIndex={-1}
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.preventDefault();
          onClose();
        }
      }}
      className="space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-4 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600"
    >
      <h3 id={headingId} className="text-sm font-semibold text-slate-900">
        {PACK_DIALOG_HEADING}
      </h3>
      <div role="status" className="text-sm text-slate-700">
        {status === 'success' ? PACK_OPENING_SOON : status === 'error' ? PACK_CLICK_ERROR : null}
      </div>
      {status === 'error' && (
        <button
          type="button"
          onClick={tryAgain}
          className="min-h-11 rounded-lg bg-brand-600 px-4 text-sm font-semibold text-white hover:bg-brand-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 sm:min-h-9"
        >
          {PACK_TRY_AGAIN}
        </button>
      )}
      {status === 'success' && email !== undefined && (
        <div className="space-y-1">
          <label className="flex items-start gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={optedIn}
              onChange={(event) => {
                toggle(event.target.checked);
              }}
              className="mt-0.5 size-4 accent-brand-600"
            />
            <span>{optInLabel(email)}</span>
          </label>
          <p role="status" className="text-sm text-slate-600">
            {optIn.isPending ? OPT_IN_SAVING : null}
          </p>
          {optIn.isError && <p className="text-sm text-danger-600">{OPT_IN_ERROR}</p>}
        </div>
      )}
      <button
        type="button"
        onClick={onClose}
        className="min-h-11 rounded-lg border border-slate-300 bg-white px-4 text-sm font-semibold text-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 sm:min-h-9"
      >
        {CLOSE}
      </button>
    </div>
  );
}
