import { useQueryClient } from '@tanstack/react-query';
import { useId, useState } from 'react';
import { Link } from 'react-router';

import { ApiError } from '@/api/client';
import { focusOnMount } from '@/components/ui/focusOnMount';
import { PASSWORD_HINT } from '@/features/auth/authCopy';
import { AuthErrorNotice } from '@/features/auth/components/AuthErrorNotice';

import {
  LINK_INCOMPLETE,
  LOG_IN_LABEL,
  PASSWORD_CHANGED_NOTE,
  RESET_CONFIRM_HEADING,
  RESET_CONFIRM_PENDING_LABEL,
  RESET_CONFIRM_SUBMIT_LABEL,
  RESET_LINK_INVALID,
  SEND_NEW_LINK_LABEL,
} from '../accountMailCopy';
import { useFragmentToken } from '../fragmentToken';
import {
  confirmPasswordResetMutationKey,
  useConfirmPasswordReset,
} from '../hooks/useConfirmPasswordReset';

import {
  alertClass,
  headingClass,
  inputClass,
  labelClass,
  linkClass,
  primaryButtonClass,
  sectionClass,
  statusClass,
} from './styles';

import type { SyntheticEvent } from 'react';

/**
 * `/reset-password/confirm` (AC-48) — public. Reads the token from the fragment once and strips it,
 * as `/confirm-email` does.
 *
 * - **Empty:** no token → *"This link is incomplete. …"* and no form.
 * - **Idle:** a labelled new-password field with 2.1's hint, and **Save new password**.
 * - **Pending:** *"Saving your new password…"*, disabled; a double click posts once.
 * - **204:** `role="status"` *"Your password has been changed and you've been logged out
 *   everywhere."* + **Log in**. An authenticated tab signs out with reason `password_changed`
 *   (no refresh call), and other tabs follow through 2.2's `BroadcastChannel` — that is
 *   `useConfirmPasswordReset`'s, so it happens even if this page is gone by then.
 * - **400 `link_invalid`:** *"This reset link has expired or has already been used."* + **Send a new
 *   link** → `/reset-password`. The form goes: this token can never succeed.
 * - **422 `password_*`:** the server's bound (2.1's copy), bound to the field; the token is kept
 *   (a 422 does not consume it) and the form stays.
 * - **503 / network:** distinct from pending; the button returns.
 *
 * The token is the `useFragmentToken` value for the page's whole life — in memory only, sent in a
 * JSON body only, never in a URL, a query key or storage (AC-51).
 */
export function PasswordResetConfirmPage(): React.JSX.Element {
  const token = useFragmentToken();
  const [password, setPassword] = useState('');
  const reset = useConfirmPasswordReset();
  const queryClient = useQueryClient();
  const baseId = useId();
  const passwordId = `${baseId}-password`;
  const hintId = `${baseId}-hint`;
  const errorId = `${baseId}-error`;

  const linkInvalid = reset.error instanceof ApiError && reset.error.code === 'link_invalid';

  function handleSubmit(event: SyntheticEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (token === null) {
      return;
    }
    if (queryClient.isMutating({ mutationKey: confirmPasswordResetMutationKey }) > 0) {
      return;
    }
    reset.mutate({ token, password });
  }

  function body(): React.JSX.Element {
    if (token === null) {
      return <p className="text-sm text-slate-700">{LINK_INCOMPLETE}</p>;
    }
    if (reset.isSuccess) {
      return (
        <div className="space-y-3">
          <p role="status" tabIndex={-1} ref={focusOnMount} className={statusClass}>
            {PASSWORD_CHANGED_NOTE}
          </p>
          <p className="text-sm">
            <Link to="/login" className={linkClass}>
              {LOG_IN_LABEL}
            </Link>
          </p>
        </div>
      );
    }
    if (linkInvalid) {
      return (
        <div role="alert" tabIndex={-1} ref={focusOnMount} className={`space-y-2 ${alertClass}`}>
          <p>{RESET_LINK_INVALID}</p>
          <p>
            <Link to="/reset-password" className={linkClass}>
              {SEND_NEW_LINK_LABEL}
            </Link>
          </p>
        </div>
      );
    }
    const describedBy =
      [hintId, reset.error === null ? undefined : errorId].filter(Boolean).join(' ') || undefined;
    return (
      <form onSubmit={handleSubmit} aria-busy={reset.isPending} noValidate className="space-y-4">
        <div>
          <label htmlFor={passwordId} className={labelClass}>
            New password
          </label>
          <input
            id={passwordId}
            name="password"
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(event) => {
              setPassword(event.target.value);
            }}
            disabled={reset.isPending}
            aria-describedby={describedBy}
            className={inputClass}
          />
          <p id={hintId} className="mt-1 text-sm text-slate-500">
            {PASSWORD_HINT}
          </p>
        </div>

        <AuthErrorNotice id={errorId} action="password_reset" error={reset.error} focusOnMount />

        <button type="submit" disabled={reset.isPending} className={primaryButtonClass}>
          {reset.isPending ? RESET_CONFIRM_PENDING_LABEL : RESET_CONFIRM_SUBMIT_LABEL}
        </button>
      </form>
    );
  }

  return (
    // Not `aria-labelledby` the heading, unlike the other three screens: "Choose a new password"
    // would then also be an accessible *label*, and a section labelled like the field inside it
    // makes "the new-password field" ambiguous to anyone finding it by its label.
    <section className={sectionClass}>
      <h2 className={headingClass}>{RESET_CONFIRM_HEADING}</h2>
      {body()}
    </section>
  );
}
