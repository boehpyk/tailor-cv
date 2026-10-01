/* eslint-disable @typescript-eslint/no-unused-vars -- T29 SKELETON: the parameters are the signature qa's T30 tests compile against; T31 uses them and deletes this line. */
import type { GuestWorkClaimResult } from '../types';

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
 * SKELETON (T29): renders nothing; T31 builds it.
 */
export function GuestWorkOffer(_props: GuestWorkOfferProps): React.JSX.Element | null {
  return null;
}
