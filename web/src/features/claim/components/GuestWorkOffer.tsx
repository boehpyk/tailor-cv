import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { HoldNote } from '@/features/retry/components/HoldNote';
import { useErrorHold } from '@/features/retry/useHold';

import {
  CLAIM_FAILED,
  CLAIM_NOTHING_LEFT,
  KEEP_LABEL,
  KEEP_PENDING_LABEL,
  NOT_NOW_LABEL,
  OFFER_REGION_LABEL,
  OFFER_RETENTION_LINE,
  claimMovedNothing,
  claimRateLimited,
  claimSuccessNote,
  guestWorkOfferSentence,
} from '../claimCopy';
import { claimGuestWorkMutationKey, useClaimGuestWork } from '../hooks/useClaimGuestWork';
import { useGuestWorkSummary } from '../hooks/useGuestWorkSummary';

import type { GuestWorkClaimResult } from '../types';
import type { HoldNoteProps } from '@/features/retry/components/HoldNote';

export interface GuestWorkOfferProps {
  /** The signed-in user the work would move to — roots the claim's mutation key (AC-42). */
  readonly userId: string;
  /**
   * Called once after a claim answers 200, **after** the hook's cache work (guest roots removed,
   * account root invalidated), with the server's counts. The guest run page navigates here
   * (`replace` to `/history/:id/:document`); the account workspace passes nothing and simply
   * re-reads.
   */
  readonly onClaimed?: (result: GuestWorkClaimResult) => void;
}

/**
 * The claim offer (AC-37…AC-40) — a **container**: `useGuestWorkSummary` decides whether there is
 * anything to offer, `useClaimGuestWork` does the claim.
 *
 * - **Nothing** while the guest lists load, when they are empty, 401 or failed (C-37).
 * - **Idle:** a `role="region"` naming each guest CV and the number of tailored applications, the
 *   retention sentence, **Keep them in my account** and **Not now**.
 * - **Pending:** the button reads *"Keeping your work…"*, disabled; a same-tick double click posts
 *   once (`queryClient.isMutating({ mutationKey })`).
 * - **Success:** `role="status"` *"Kept in your account: …"*; all zeros → the *"nothing left to
 *   keep"* line and the offer goes.
 * - **Failures** (`role="alert"`): 429 → *"Too many attempts…"*, the button back after
 *   `Retry-After`; 503 / network → *"Couldn't keep your work…"* + the button; 401 `not_signed_in` →
 *   2.1's sign-out path.
 * - **Not now** hides it for the page's lifetime and sends nothing (AC-40).
 *
 * The outcome is rendered from the **mutation's** state, never from the guest lists: after a claim
 * those lists are about a session that no longer exists. That is also why the lists are read in
 * `OfferRegion`, a child that **unmounts** on success: an observer still mounted after the claim's
 * `removeQueries` would re-create the guest entries it just removed (and re-read them with a cookie
 * that names nothing).
 */
export function GuestWorkOffer({
  userId,
  onClaimed,
}: GuestWorkOfferProps): React.JSX.Element | null {
  const claim = useClaimGuestWork(userId);
  const queryClient = useQueryClient();
  const [dismissed, setDismissed] = useState(false);
  // Slice 3.3 (AC-14): the shared hold, which replaced this component's private copy of it.
  const hold = useErrorHold(claim.error);

  if (claim.isSuccess) {
    return (
      <p
        role="status"
        className="mb-6 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-900"
      >
        {claimMovedNothing(claim.data) ? CLAIM_NOTHING_LEFT : claimSuccessNote(claim.data)}
      </p>
    );
  }
  if (dismissed) {
    return null;
  }

  function keep(): void {
    // `isPending` lags a same-tick double click; the mutation cache does not (CLAUDE.md, 2.1).
    if (queryClient.isMutating({ mutationKey: claimGuestWorkMutationKey(userId) }) > 0) {
      return;
    }
    claim.mutate(undefined, {
      onSuccess: (result) => {
        onClaimed?.(result);
      },
    });
  }

  return (
    <OfferRegion
      isPending={claim.isPending}
      keepDisabled={claim.isPending || hold.held}
      alert={
        claim.error === null
          ? null
          : hold.deadlineMs === null
            ? CLAIM_FAILED
            : claimRateLimited(hold.phrase)
      }
      hold={hold}
      onKeep={keep}
      onNotNow={() => {
        setDismissed(true);
      }}
    />
  );
}

interface OfferRegionProps {
  readonly isPending: boolean;
  readonly keepDisabled: boolean;
  /** The failure sentence (`role="alert"`), or `null` when the last attempt did not fail. */
  readonly alert: string | null;
  readonly hold: HoldNoteProps;
  readonly onKeep: () => void;
  readonly onNotNow: () => void;
}

/** The idle / pending / failed offer — or nothing, when the guest lists hold nothing to offer. */
function OfferRegion({
  isPending,
  keepDisabled,
  alert,
  hold,
  onKeep,
  onNotNow,
}: OfferRegionProps): React.JSX.Element | null {
  const summary = useGuestWorkSummary();
  if (summary === null) {
    return null;
  }

  return (
    <section
      aria-label={OFFER_REGION_LABEL}
      className="mb-6 space-y-2 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm"
    >
      <p className="font-medium text-amber-950">{guestWorkOfferSentence(summary)}</p>
      <p className="text-amber-900">{OFFER_RETENTION_LINE}</p>
      <div className="flex flex-wrap items-center gap-3 pt-1">
        <button
          type="button"
          onClick={onKeep}
          disabled={keepDisabled}
          className="rounded-md bg-slate-900 px-3 py-1.5 font-medium text-white disabled:opacity-60"
        >
          {isPending ? KEEP_PENDING_LABEL : KEEP_LABEL}
        </button>
        <button
          type="button"
          onClick={onNotNow}
          className="text-slate-600 underline underline-offset-2"
        >
          {NOT_NOW_LABEL}
        </button>
      </div>
      {alert !== null && (
        <p role="alert" className="text-red-800">
          {alert}
        </p>
      )}
      <HoldNote
        held={hold.held}
        remainingSeconds={hold.remainingSeconds}
        released={hold.released}
      />
    </section>
  );
}
