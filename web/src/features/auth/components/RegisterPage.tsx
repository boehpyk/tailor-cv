import { useQueryClient } from '@tanstack/react-query';
import { Link, Navigate, useSearchParams } from 'react-router';

import {
  PASSWORD_HINT,
  REGISTER_GUEST_WORK_NOTICE,
  REGISTER_PENDING_LABEL,
  REGISTER_STORAGE_NOTICE,
  REGISTER_SUBMIT_LABEL,
} from '../authCopy';
import { useAuth } from '../hooks/useAuth';
import { registerMutationKey, useRegister } from '../hooks/useRegister';
import { safeNext } from '../safeNext';

import { CredentialsForm } from './CredentialsForm';

import type { Credentials } from '../types';

/**
 * `/register` (AC-40, AC-43, AC-45). The same shape as `LoginPage` — a `201` is also a login, so
 * success is the page's "authenticated visitors don't belong here" guard taking effect.
 *
 * The two static notices are part of the idle state, not small print: what is stored, and what
 * happens to the work the visitor did as a guest (AC-40).
 *
 * **Sign in** links back to `/login` with the same query string, so a `next` that brought the
 * visitor here survives the cross-link both ways (slice 2.4, AC-36) — `safeNext` still judges it on
 * arrival. Without a `next` the link is exactly `/login`.
 */
export function RegisterPage() {
  const auth = useAuth();
  const [searchParams] = useSearchParams();
  const register = useRegister();
  const queryClient = useQueryClient();

  if (auth.status === 'authenticated') {
    return <Navigate to={safeNext(searchParams.get('next'))} replace />;
  }

  function submit(credentials: Credentials): void {
    // I-53 — see `LoginPage.submit` for why the cache is asked rather than `register.isPending`.
    if (queryClient.isMutating({ mutationKey: registerMutationKey }) > 0) {
      return;
    }
    register.mutate(credentials);
  }

  return (
    <section aria-labelledby="register-heading" className="mb-10 max-w-sm">
      <h2 id="register-heading" className="mb-6 text-lg font-semibold text-slate-900">
        Create an account
      </h2>
      <CredentialsForm
        action="register"
        passwordAutoComplete="new-password"
        submitLabel={REGISTER_SUBMIT_LABEL}
        pendingLabel={REGISTER_PENDING_LABEL}
        passwordHint={PASSWORD_HINT}
        isPending={register.isPending}
        error={register.error}
        onSubmit={submit}
      >
        <div className="space-y-2 text-sm text-slate-600">
          <p>{REGISTER_STORAGE_NOTICE}</p>
          <p>{REGISTER_GUEST_WORK_NOTICE}</p>
          <p>
            Already have an account?{' '}
            <Link
              to={{ pathname: '/login', search: searchParams.toString() }}
              className="font-medium text-slate-900 underline underline-offset-2"
            >
              Sign in
            </Link>
          </p>
        </div>
      </CredentialsForm>
    </section>
  );
}
