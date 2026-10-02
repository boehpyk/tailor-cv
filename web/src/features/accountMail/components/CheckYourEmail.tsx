/* eslint-disable @typescript-eslint/no-unused-vars -- T36 SKELETON: the parameters are the signature qa's T37 tests compile against; T38 uses them and deletes this line. */
import type { Credentials } from '@/features/auth/types';

export interface CheckYourEmailProps {
  /**
   * What the visitor just registered with — held in `RegisterPage`'s own state, never in the cache
   * or storage (AC-51). The email is shown; both are re-posted by **Send it again**.
   */
  readonly credentials: Credentials;
  /**
   * The raw `next` search parameter `/register` arrived with, or `null`. **Already confirmed? Log
   * in** carries it to `/login?next=…`, where `safeNext` judges it (AC-50); nothing here does.
   */
  readonly next: string | null;
  /**
   * Whether this browser has guest work (the guest scope only). When true, the 24-hour deadline
   * sentence is shown (AC-45). The page decides — `useGuestWorkSummary` — and passes the answer.
   */
  readonly hasGuestWork: boolean;
  /** **Use a different email**: back to the form, the email kept and the password cleared. */
  readonly onUseDifferentEmail: () => void;
}

/**
 * What replaces the register form on a 202 (AC-45) — not a route: the credentials live in the
 * page's state for **Send it again**, and a reload loses them on purpose.
 *
 * - **Idle:** a `role="status"` region: *"Check your inbox at {email}. …"*, the mail-provider
 *   sentence, the guest-work deadline when `hasGuestWork`, **Send it again**, **Use a different
 *   email** and **Already confirmed? Log in** → `/login?next=<next>`.
 * - **Send it again pending:** *"Sending…"*, disabled; a same-tick double click posts once
 *   (`isMutating` on `requestRegistrationMutationKey`).
 * - **Sent again:** `role="status"` *"Sent again."*
 * - **Failures** (`role="alert"`): 429 with the server's seconds, then re-enabled (V-61);
 *   503 / network → *"Couldn't send just now — try again."*
 *
 * SKELETON (T36): renders nothing; T38 builds it.
 */
export function CheckYourEmail(_props: CheckYourEmailProps): React.JSX.Element | null {
  return null;
}
