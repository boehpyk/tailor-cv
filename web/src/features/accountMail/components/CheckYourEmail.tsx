import { useQueryClient } from '@tanstack/react-query';
import { useId } from 'react';
import { Link } from 'react-router';

import { ApiError } from '@/api/client';
import { focusOnMount } from '@/components/ui/focusOnMount';
import { authErrorCopy } from '@/features/auth/authCopy';

import {
  ALREADY_CONFIRMED_PROMPT,
  CHECK_EMAIL_HEADING,
  GUEST_WORK_DEADLINE_NOTE,
  LOG_IN_LABEL,
  MAIL_PROVIDER_SENTENCE,
  SENDING_LABEL,
  SEND_AGAIN_FAILED,
  SEND_AGAIN_LABEL,
  SENT_AGAIN_NOTE,
  USE_DIFFERENT_EMAIL_LABEL,
  checkInboxSentence,
} from '../accountMailCopy';
import {
  requestRegistrationMutationKey,
  useRequestRegistration,
} from '../hooks/useRequestRegistration';

import {
  alertClass,
  headingClass,
  linkClass,
  primaryButtonClass,
  secondaryButtonClass,
  sectionClass,
} from './styles';

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
 * **Every outcome is the send-again mutation's own state**, read during render — "sent again" and
 * "failed" are its `isSuccess` / `error`, so they can never both show. The copy is the same for
 * every address: the 202 does not say whether the address already had an account, and neither
 * may anything on this screen.
 */
export function CheckYourEmail({
  credentials,
  next,
  hasGuestWork,
  onUseDifferentEmail,
}: CheckYourEmailProps): React.JSX.Element {
  const sendAgain = useRequestRegistration();
  const queryClient = useQueryClient();
  const headingId = useId();

  function resend(): void {
    // 2.1's I-53 trap: `sendAgain.isPending` lags a same-tick double click; the cache does not.
    if (queryClient.isMutating({ mutationKey: requestRegistrationMutationKey }) > 0) {
      return;
    }
    sendAgain.mutate(credentials);
  }

  // `next` travels verbatim: `/login` judges it with `safeNext` on arrival (AC-50).
  const loginSearch = next === null ? '' : new URLSearchParams({ next }).toString();

  return (
    <section aria-labelledby={headingId} className={sectionClass}>
      <h2 id={headingId} tabIndex={-1} ref={focusOnMount} className={headingClass}>
        {CHECK_EMAIL_HEADING}
      </h2>

      <div role="status" className="space-y-2 text-sm text-slate-700">
        <p>{checkInboxSentence(credentials.email)}</p>
        <p className="text-slate-600">{MAIL_PROVIDER_SENTENCE}</p>
      </div>

      {hasGuestWork && (
        <p className="mt-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          {GUEST_WORK_DEADLINE_NOTE}
        </p>
      )}

      <div className="mt-6 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={resend}
          disabled={sendAgain.isPending}
          className={primaryButtonClass}
        >
          {sendAgain.isPending ? SENDING_LABEL : SEND_AGAIN_LABEL}
        </button>
        <button type="button" onClick={onUseDifferentEmail} className={secondaryButtonClass}>
          {USE_DIFFERENT_EMAIL_LABEL}
        </button>
      </div>

      {sendAgain.isSuccess && (
        <p role="status" className="mt-3 text-sm text-emerald-800">
          {SENT_AGAIN_NOTE}
        </p>
      )}
      {sendAgain.error !== null && (
        <p role="alert" tabIndex={-1} ref={focusOnMount} className={`mt-3 ${alertClass}`}>
          {sendAgainFailure(sendAgain.error)}
        </p>
      )}

      <p className="mt-6 text-sm text-slate-600">
        <span>{ALREADY_CONFIRMED_PROMPT}</span>{' '}
        <Link to={{ pathname: '/login', search: loginSearch }} className={linkClass}>
          {LOG_IN_LABEL}
        </Link>
      </p>
    </section>
  );
}

/**
 * The sentence for a failed **Send it again**. A 429 carries the server's own seconds (2.1's
 * `tooManyAttempts` copy, V-61); "not right now" — a 5xx or no answer at all — is one sentence that
 * says nothing was sent and the button can be pressed again (V-66). Anything else (a 403 after a
 * deploy, say) reads as `/register`'s own refusal would.
 */
function sendAgainFailure(error: Error): string {
  if (!(error instanceof ApiError) || error.status >= 500) {
    return SEND_AGAIN_FAILED;
  }
  return authErrorCopy('register', error).message;
}
