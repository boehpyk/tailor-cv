import { useQueryClient } from '@tanstack/react-query';
import { useId, useState } from 'react';
import { useNavigate } from 'react-router';

import {
  ACCOUNT_DELETED_NOTICE,
  DELETE_ACCOUNT_CONFIRM_LABEL,
  DELETE_ACCOUNT_HEADING,
  DELETE_ACCOUNT_PASSWORD_LABEL,
  DELETE_ACCOUNT_PENDING_LABEL,
  DELETE_ACCOUNT_SUBMIT_LABEL,
  DELETE_ACCOUNT_WHAT_GOES_NOTE,
  DELETE_ACCOUNT_WHAT_STAYS_NOTE,
  deleteAccountErrorCopy,
} from '@/features/savedCvs/savedCvsCopy';

import { deleteAccountMutationKey, useDeleteAccount } from '../hooks/useDeleteAccount';
import { homeNoticeState } from '../homeNotice';

/**
 * The Delete account section on `/account` (AC-40, AC-46).
 *
 * States what goes (the account, the saved CVs and their files, every signed-in device) and what
 * does not go at once (workspace copies, within 24 hours). A labelled password field
 * (`autocomplete="current-password"`), an "I understand this can't be undone" checkbox that gates
 * the button, "Deleting your account…" while pending, and each refusal in its own words — none of
 * which signs the user out, because none of them deleted anything. On success it navigates to `/`
 * carrying the notice "Your account and saved CVs were deleted." in the router's location state;
 * `useDeleteAccount` then signs this tab out and tells the others.
 *
 * The password and the checkbox are local form state; the password is never put anywhere else.
 */
export function DeleteAccountSection(): React.JSX.Element {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const deletion = useDeleteAccount(() => {
    void navigate('/', { state: homeNoticeState(ACCOUNT_DELETED_NOTICE) });
  });
  const [password, setPassword] = useState('');
  const [understood, setUnderstood] = useState(false);
  const headingId = useId();
  const passwordId = useId();
  const confirmId = useId();
  const errorId = useId();

  function handleSubmit(event: React.SyntheticEvent<HTMLFormElement>): void {
    event.preventDefault();
    // `isPending` lags a same-tick double submit; the mutation cache does not (CLAUDE.md).
    if (
      !understood ||
      password === '' ||
      queryClient.isMutating({ mutationKey: deleteAccountMutationKey }) > 0
    ) {
      return;
    }
    deletion.mutate(password);
  }

  const error = deletion.error;

  return (
    <section aria-labelledby={headingId} className="mt-10 border-t border-slate-200 pt-6">
      <h2 id={headingId} className="mb-3 text-lg font-semibold text-slate-900">
        {DELETE_ACCOUNT_HEADING}
      </h2>
      <div className="mb-4 space-y-2 text-sm text-slate-600">
        <p>{DELETE_ACCOUNT_WHAT_GOES_NOTE}</p>
        <p>{DELETE_ACCOUNT_WHAT_STAYS_NOTE}</p>
      </div>
      <form onSubmit={handleSubmit} className="space-y-3">
        <div className="space-y-1">
          <label htmlFor={passwordId} className="block text-sm font-medium text-slate-700">
            {DELETE_ACCOUNT_PASSWORD_LABEL}
          </label>
          <input
            id={passwordId}
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => {
              setPassword(event.target.value);
            }}
            aria-describedby={error === null ? undefined : errorId}
            aria-invalid={error === null ? undefined : true}
            className="w-full max-w-sm rounded-md border border-slate-300 px-3 py-2 text-sm"
          />
        </div>
        <div className="flex items-center gap-2">
          <input
            id={confirmId}
            type="checkbox"
            checked={understood}
            onChange={(event) => {
              setUnderstood(event.target.checked);
            }}
          />
          <label htmlFor={confirmId} className="text-sm text-slate-700">
            {DELETE_ACCOUNT_CONFIRM_LABEL}
          </label>
        </div>
        {error !== null && (
          <p id={errorId} role="alert" className="text-sm text-red-700">
            {deleteAccountErrorCopy(error)}
          </p>
        )}
        <button
          type="submit"
          disabled={!understood || password === '' || deletion.isPending}
          className="rounded-md bg-red-700 px-4 py-2 text-sm font-medium text-white disabled:opacity-60"
        >
          {deletion.isPending ? DELETE_ACCOUNT_PENDING_LABEL : DELETE_ACCOUNT_SUBMIT_LABEL}
        </button>
      </form>
    </section>
  );
}
