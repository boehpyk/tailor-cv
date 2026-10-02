import { useQueryClient } from '@tanstack/react-query';
import { useId } from 'react';
import { Link } from 'react-router';

import { ApiError } from '@/api/client';
import { focusOnMount } from '@/components/ui/focusOnMount';

import {
  CONFIRMING_LABEL,
  CONFIRM_ALREADY_REGISTERED,
  CONFIRM_EMAIL_HEADING,
  CONFIRM_EMAIL_LABEL,
  CONFIRM_LINK_INVALID,
  CONFIRM_UNAVAILABLE,
  CREATE_ACCOUNT_LABEL,
  EMAIL_CONFIRMED_NOTE,
  LINK_INCOMPLETE,
  LOG_IN_LABEL,
  RESET_PASSWORD_LINK_LABEL,
} from '../accountMailCopy';
import { useFragmentToken } from '../fragmentToken';
import {
  confirmRegistrationMutationKey,
  useConfirmRegistration,
} from '../hooks/useConfirmRegistration';

import {
  alertClass,
  headingClass,
  linkClass,
  primaryButtonClass,
  sectionClass,
  statusClass,
} from './styles';

/**
 * What a failed confirmation means to the person holding the link. Three outcomes, three ways
 * forward, decided by `code` (never by the server's prose):
 *
 * - `link_invalid` — the link is dead (expired, used, never issued: one code on purpose). Retrying
 *   cannot help, so the button goes and the two real next steps take its place.
 * - `already_registered` — the address became an account meanwhile. Same: log in, or reset.
 * - `unavailable` — a 5xx, no answer, or anything unexpected. **Nothing changed**, the token is
 *   still in memory, and the button stays for another try.
 */
type ConfirmFailure = 'link_invalid' | 'already_registered' | 'unavailable';

function classify(error: Error): ConfirmFailure {
  if (error instanceof ApiError) {
    if (error.code === 'link_invalid') {
      return 'link_invalid';
    }
    if (error.code === 'email_already_registered') {
      return 'already_registered';
    }
  }
  return 'unavailable';
}

/**
 * `/confirm-email` (AC-46) — public. Reads the token from the link's fragment once
 * (`useFragmentToken`) and strips it before paint.
 *
 * - **Empty:** no token → *"This link is incomplete. …"* and no button; no request (V-62, V-63).
 * - **Idle:** **Confirm my email address** — confirmation waits for a click (OQ-4), so a mail
 *   scanner prefetching the link confirms nothing.
 * - **Pending:** *"Confirming…"*, disabled; a double click posts once.
 * - **204:** `role="status"` *"Your email address is confirmed."* + **Log in** → `/login`. No sign-in.
 * - **400 `link_invalid`:** the expired-or-used sentence + **Log in** and **Create an account**.
 * - **409 `email_already_registered`:** the already-an-account sentence + **Log in** and **Reset your
 *   password**.
 * - **503 / network:** *"Couldn't confirm just now. Nothing changed — try again."* + the button,
 *   the token still in memory.
 *
 * The request carries no bearer (`confirmRegistration` never asks for one) and its success touches
 * no auth state: a visitor signed in as someone else stays exactly that (V-65).
 */
export function ConfirmEmailPage(): React.JSX.Element {
  const token = useFragmentToken();
  const confirm = useConfirmRegistration();
  const queryClient = useQueryClient();
  const headingId = useId();

  function submit(presented: string): void {
    // 2.1's I-53 trap: the mutation cache, not `isPending`, holds inside a single tick.
    if (queryClient.isMutating({ mutationKey: confirmRegistrationMutationKey }) > 0) {
      return;
    }
    confirm.mutate(presented);
  }

  const failure = confirm.error === null ? null : classify(confirm.error);
  // The button stays for every state that another click could change: idle, pending, unavailable.
  const canStillConfirm =
    !confirm.isSuccess && failure !== 'link_invalid' && failure !== 'already_registered';

  return (
    <section aria-labelledby={headingId} className={sectionClass}>
      <h2 id={headingId} className={headingClass}>
        {CONFIRM_EMAIL_HEADING}
      </h2>

      {token === null && <p className="text-sm text-slate-700">{LINK_INCOMPLETE}</p>}

      {confirm.isSuccess && (
        <div className="space-y-3">
          <p role="status" tabIndex={-1} ref={focusOnMount} className={statusClass}>
            {EMAIL_CONFIRMED_NOTE}
          </p>
          <p className="text-sm">
            <Link to="/login" className={linkClass}>
              {LOG_IN_LABEL}
            </Link>
          </p>
        </div>
      )}

      {failure !== null && <ConfirmFailureNotice failure={failure} />}

      {token !== null && canStillConfirm && (
        <button
          type="button"
          onClick={() => {
            submit(token);
          }}
          disabled={confirm.isPending}
          className={`mt-4 ${primaryButtonClass}`}
        >
          {confirm.isPending ? CONFIRMING_LABEL : CONFIRM_EMAIL_LABEL}
        </button>
      )}
    </section>
  );
}

function ConfirmFailureNotice({ failure }: { readonly failure: ConfirmFailure }) {
  return (
    <div role="alert" tabIndex={-1} ref={focusOnMount} className={`space-y-2 ${alertClass}`}>
      {failure === 'link_invalid' && (
        <>
          <p>{CONFIRM_LINK_INVALID}</p>
          <p className="flex gap-4">
            <Link to="/login" className={linkClass}>
              {LOG_IN_LABEL}
            </Link>
            <Link to="/register" className={linkClass}>
              {CREATE_ACCOUNT_LABEL}
            </Link>
          </p>
        </>
      )}
      {failure === 'already_registered' && (
        <>
          <p>{CONFIRM_ALREADY_REGISTERED}</p>
          <p className="flex gap-4">
            <Link to="/login" className={linkClass}>
              {LOG_IN_LABEL}
            </Link>
            <Link to="/reset-password" className={linkClass}>
              {RESET_PASSWORD_LINK_LABEL}
            </Link>
          </p>
        </>
      )}
      {failure === 'unavailable' && <p>{CONFIRM_UNAVAILABLE}</p>}
    </div>
  );
}
