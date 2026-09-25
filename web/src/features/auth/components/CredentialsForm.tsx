import { useId, useState } from 'react';

import { AuthErrorNotice } from './AuthErrorNotice';

import type { AuthAction } from '../authCopy';
import type { Credentials } from '../types';
import type { ReactNode, SyntheticEvent } from 'react';

export interface CredentialsFormProps {
  /** Which form this is — decides the error copy. */
  readonly action: Exclude<AuthAction, 'logout'>;
  /**
   * `current-password` on `/login`, `new-password` on `/register` (AC-45). This one attribute is
   * the difference between a password manager offering to *fill* and offering to *generate*.
   */
  readonly passwordAutoComplete: 'current-password' | 'new-password';
  readonly submitLabel: string;
  readonly pendingLabel: string;
  /** Shown under the password field and linked to it by `aria-describedby`. */
  readonly passwordHint?: string;
  /** The mutation's own state, read — never copied into this component. */
  readonly isPending: boolean;
  readonly error: Error | null;
  readonly onSubmit: (credentials: Credentials) => void;
  /** Anything the page puts below the button (notices, a link to the other form). */
  readonly children?: ReactNode;
}

/**
 * The email-and-password form both auth pages render. Presentational: it owns what the user is
 * typing (local `useState`, the one kind of state that belongs here) and nothing else. The request,
 * its pending state and its error belong to the page's mutation and arrive as props.
 *
 * **Submitting disables everything** (AC-39) — the inputs so the credentials sent are the ones on
 * screen, and the button so there is no second request. The disabled button is the visible half
 * of I-53; the page's `onSubmit` guard is the half that holds inside a single tick (see
 * `LoginPage`).
 *
 * **One error notice, linked from both inputs** by `aria-describedby` (AC-45): a screen-reader user
 * who returns to a field hears why the last attempt failed, not only at the moment it did.
 */
export function CredentialsForm({
  action,
  passwordAutoComplete,
  submitLabel,
  pendingLabel,
  passwordHint,
  isPending,
  error,
  onSubmit,
  children,
}: CredentialsFormProps) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const baseId = useId();
  const emailId = `${baseId}-email`;
  const passwordId = `${baseId}-password`;
  const hintId = `${baseId}-password-hint`;
  const errorId = `${baseId}-error`;

  const errorRef = error === null ? undefined : errorId;
  const passwordDescribedBy =
    [passwordHint === undefined ? undefined : hintId, errorRef].filter(Boolean).join(' ') ||
    undefined;

  function handleSubmit(event: SyntheticEvent<HTMLFormElement>): void {
    event.preventDefault();
    onSubmit({ email, password });
  }

  const inputClass =
    'mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-slate-900 ' +
    'focus:border-slate-500 focus:outline-none disabled:bg-slate-100 disabled:text-slate-500';

  return (
    <form onSubmit={handleSubmit} aria-busy={isPending} noValidate className="space-y-4">
      <div>
        <label htmlFor={emailId} className="block text-sm font-medium text-slate-700">
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
          disabled={isPending}
          aria-describedby={errorRef}
          className={inputClass}
        />
      </div>

      <div>
        <label htmlFor={passwordId} className="block text-sm font-medium text-slate-700">
          Password
        </label>
        <input
          id={passwordId}
          name="password"
          type="password"
          autoComplete={passwordAutoComplete}
          required
          value={password}
          onChange={(event) => {
            setPassword(event.target.value);
          }}
          disabled={isPending}
          aria-describedby={passwordDescribedBy}
          className={inputClass}
        />
        {passwordHint !== undefined && (
          <p id={hintId} className="mt-1 text-sm text-slate-500">
            {passwordHint}
          </p>
        )}
      </div>

      <AuthErrorNotice id={errorId} action={action} error={error} />

      <button
        type="submit"
        disabled={isPending}
        className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-60"
      >
        {isPending ? pendingLabel : submitLabel}
      </button>

      {children}
    </form>
  );
}
