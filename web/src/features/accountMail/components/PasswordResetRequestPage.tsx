import { useQueryClient } from '@tanstack/react-query';
import { useId, useState } from 'react';

import { focusOnMount } from '@/components/ui/focusOnMount';
import { AuthErrorNotice } from '@/features/auth/components/AuthErrorNotice';

import {
  MAIL_PROVIDER_SENTENCE,
  RESET_REQUEST_HEADING,
  RESET_REQUEST_SUBMIT_LABEL,
  SENDING_LABEL,
  resetRequestedSentence,
} from '../accountMailCopy';
import {
  requestPasswordResetMutationKey,
  useRequestPasswordReset,
} from '../hooks/useRequestPasswordReset';

import {
  headingClass,
  inputClass,
  labelClass,
  primaryButtonClass,
  sectionClass,
  statusClass,
} from './styles';

import type { SyntheticEvent } from 'react';

/**
 * `/reset-password` (AC-47) — public; linked from `/login`'s **Forgot your password?**.
 *
 * - **Idle:** a labelled email field and **Send reset link**, with the mail-provider sentence.
 * - **Pending:** *"Sending…"*, disabled; a same-tick double click posts once (`isMutating` on
 *   `requestPasswordResetMutationKey`, V-60).
 * - **202:** `role="status"` *"If there's an account for {email}, …"* + the mail-provider sentence —
 *   the same whether or not the account exists.
 * - **Failures** (`role="alert"`), each distinct: 422 `invalid_email`, 429 with the server's
 *   seconds, 503, network — 2.1's `authErrorCopy`, under the `password_reset` action.
 *
 * The address in the success sentence is the one the request was **sent** with — the mutation's
 * own `variables` — not the field, which the visitor may already be editing.
 */
export function PasswordResetRequestPage(): React.JSX.Element {
  const [email, setEmail] = useState('');
  const request = useRequestPasswordReset();
  const queryClient = useQueryClient();
  const baseId = useId();
  const headingId = `${baseId}-heading`;
  const emailId = `${baseId}-email`;
  const errorId = `${baseId}-error`;

  function handleSubmit(event: SyntheticEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (queryClient.isMutating({ mutationKey: requestPasswordResetMutationKey }) > 0) {
      return;
    }
    request.mutate(email);
  }

  return (
    <section aria-labelledby={headingId} className={sectionClass}>
      <h2 id={headingId} className={headingClass}>
        {RESET_REQUEST_HEADING}
      </h2>

      <form onSubmit={handleSubmit} aria-busy={request.isPending} noValidate className="space-y-4">
        <div>
          <label htmlFor={emailId} className={labelClass}>
            Email
          </label>
          <input
            id={emailId}
            name="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => {
              setEmail(event.target.value);
            }}
            disabled={request.isPending}
            aria-describedby={request.error === null ? undefined : errorId}
            className={inputClass}
          />
        </div>

        <AuthErrorNotice id={errorId} action="password_reset" error={request.error} focusOnMount />

        {request.isSuccess && (
          <div
            role="status"
            tabIndex={-1}
            ref={focusOnMount}
            className={`space-y-2 ${statusClass}`}
          >
            <p>{resetRequestedSentence(request.variables)}</p>
            <p>{MAIL_PROVIDER_SENTENCE}</p>
          </div>
        )}

        <button type="submit" disabled={request.isPending} className={primaryButtonClass}>
          {request.isPending ? SENDING_LABEL : RESET_REQUEST_SUBMIT_LABEL}
        </button>

        {!request.isSuccess && <p className="text-sm text-slate-600">{MAIL_PROVIDER_SENTENCE}</p>}
      </form>
    </section>
  );
}
