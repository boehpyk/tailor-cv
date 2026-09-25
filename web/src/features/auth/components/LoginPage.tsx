import { useQueryClient } from '@tanstack/react-query';
import { Link, Navigate, useSearchParams } from 'react-router';

import { LOGIN_PENDING_LABEL, LOGIN_SUBMIT_LABEL } from '../authCopy';
import { useAuth } from '../hooks/useAuth';
import { loginMutationKey, useLogin } from '../hooks/useLogin';
import { safeNext } from '../safeNext';

import { CredentialsForm } from './CredentialsForm';

import type { Credentials } from '../types';

/**
 * `/login` (AC-39, AC-43, AC-45).
 *
 * **Success and "already logged in" are one rule, not two.** A successful login flips the store to
 * `authenticated`; the page then re-renders and its guard — *an authenticated visitor does not
 * belong on this page* — sends them to `safeNext(next)`. So there is no `onSuccess: navigate(...)`
 * to disagree with the guard, and the `replace` means Back does not return to a form that would
 * only bounce them forward again.
 *
 * `next` is attacker-chosen (it arrives in a link), so it only ever reaches `Navigate` through
 * `safeNext` (AC-43).
 */
export function LoginPage() {
  const auth = useAuth();
  const [searchParams] = useSearchParams();
  const login = useLogin();
  const queryClient = useQueryClient();

  if (auth.status === 'authenticated') {
    return <Navigate to={safeNext(searchParams.get('next'))} replace />;
  }

  function submit(credentials: Credentials): void {
    // I-53. `login.isPending` is this render's value: two clicks inside one tick both see `false`,
    // because TanStack Query notifies React on its next scheduled flush. The mutation cache itself
    // is updated synchronously by `mutate`, so asking it is the check that holds within the tick —
    // and it reads the one source of truth rather than a `useRef` copy of it.
    if (queryClient.isMutating({ mutationKey: loginMutationKey }) > 0) {
      return;
    }
    login.mutate(credentials);
  }

  return (
    <section aria-labelledby="login-heading" className="mb-10 max-w-sm">
      <h2 id="login-heading" className="mb-6 text-lg font-semibold text-slate-900">
        Log in
      </h2>
      <CredentialsForm
        action="login"
        passwordAutoComplete="current-password"
        submitLabel={LOGIN_SUBMIT_LABEL}
        pendingLabel={LOGIN_PENDING_LABEL}
        isPending={login.isPending}
        error={login.error}
        onSubmit={submit}
      >
        <p className="text-sm text-slate-600">
          New here?{' '}
          <Link
            to={{ pathname: '/register', search: searchParams.toString() }}
            className="font-medium text-slate-900 underline underline-offset-2"
          >
            Create an account
          </Link>
        </p>
      </CredentialsForm>
    </section>
  );
}
