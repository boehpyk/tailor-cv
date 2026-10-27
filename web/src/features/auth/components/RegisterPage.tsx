import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Link, Navigate, useSearchParams } from 'react-router';

import { MAIL_PROVIDER_SENTENCE, SENDING_LABEL } from '@/features/accountMail/accountMailCopy';
import { cardClass, headingClass, linkClass } from '@/features/accountMail/components/styles';
import { CheckYourEmail } from '@/features/accountMail/components/CheckYourEmail';
import {
  requestRegistrationMutationKey,
  useRequestRegistration,
} from '@/features/accountMail/hooks/useRequestRegistration';
import { useGuestWorkSummary } from '@/features/claim/hooks/useGuestWorkSummary';

import {
  PASSWORD_HINT,
  REGISTER_GUEST_WORK_NOTICE,
  REGISTER_PANEL_HEADING,
  REGISTER_STORAGE_NOTICE,
  REGISTER_SUBMIT_LABEL,
} from '../authCopy';
import { useAuth } from '../hooks/useAuth';
import { safeNext } from '../safeNext';

import { CredentialsForm } from './CredentialsForm';

import type { Credentials } from '../types';

/**
 * `/register` (AC-40, AC-43, AC-45; slice 2.5's AC-45).
 *
 * **Registering no longer signs in** (slice 2.5, ADR-0027). A `202` means "if this address can
 * have an account, a mail is on its way" — the same answer for every address — so the form is
 * replaced in place by `CheckYourEmail`, and the URL does not change. The credentials the visitor
 * typed are this page's **local state** (`sent`) so **Send it again** can re-post them; they are
 * never in the query cache, the URL or storage (AC-51), and a reload loses them on purpose.
 * **Use a different email** clears `sent` (the password with it) and remounts the form with the
 * address filled in.
 *
 * The authenticated guard stays: a visitor who *is* signed in (in this tab, by any route) does not
 * belong here and goes on to `safeNext(next)`.
 *
 * The two static notices are part of the idle state, not small print: what is stored, what happens
 * to the work the visitor did as a guest (AC-40), and who sends the mail (OQ-8).
 *
 * **Sign in** links back to `/login` with the same query string, so a `next` that brought the
 * visitor here survives the cross-link both ways (slice 2.4, AC-36) — `safeNext` still judges it on
 * arrival. Without a `next` the link is exactly `/login`.
 */
export function RegisterPage() {
  const auth = useAuth();
  const [searchParams] = useSearchParams();
  const register = useRequestRegistration();
  const queryClient = useQueryClient();
  const [sent, setSent] = useState<Credentials | null>(null);
  const [formEmail, setFormEmail] = useState('');

  if (auth.status === 'authenticated') {
    return <Navigate to={safeNext(searchParams.get('next'))} replace />;
  }

  if (sent !== null) {
    return (
      <SentState
        credentials={sent}
        next={searchParams.get('next')}
        onUseDifferentEmail={() => {
          setFormEmail(sent.email);
          setSent(null);
          // The form comes back clean: no stale refusal from before the 202.
          register.reset();
        }}
      />
    );
  }

  function submit(credentials: Credentials): void {
    // I-53 — see `LoginPage.submit` for why the cache is asked rather than `register.isPending`.
    if (queryClient.isMutating({ mutationKey: requestRegistrationMutationKey }) > 0) {
      return;
    }
    register.mutate(credentials, {
      onSuccess: () => {
        setSent(credentials);
      },
    });
  }

  return (
    <section
      aria-labelledby="register-heading"
      className="mb-10 flex flex-col gap-6 md:grid md:grid-cols-[minmax(0,26rem)_1fr] md:items-start md:gap-10"
    >
      <div className={`max-md:order-2 ${cardClass}`}>
        <h2 id="register-heading" className={headingClass}>
          Create an account
        </h2>
        <CredentialsForm
          action="register"
          initialEmail={formEmail}
          passwordAutoComplete="new-password"
          submitLabel={REGISTER_SUBMIT_LABEL}
          pendingLabel={SENDING_LABEL}
          passwordHint={PASSWORD_HINT}
          isPending={register.isPending}
          error={register.error}
          onSubmit={submit}
        >
          <div className="space-y-2 text-sm text-slate-600">
            <p>{REGISTER_STORAGE_NOTICE}</p>
            <p>{MAIL_PROVIDER_SENTENCE}</p>
            <p>
              Already have an account?{' '}
              <Link
                to={{ pathname: '/login', search: searchParams.toString() }}
                className={linkClass}
              >
                Sign in
              </Link>
            </p>
          </div>
        </CredentialsForm>
      </div>

      {/* The panel (C-8, OQ-12): static text and nothing else — no input, no button, no claim
          link, nothing ticked, no request. What this browser's guest work is, and whether to keep
          it, is asked by `GuestWorkOffer` after sign-in, by name (ADR-0025). Below `md` it sits
          above the form (1i) by CSS order: one DOM, so the sentence is on the page once. */}
      <div className="flex flex-col gap-4 rounded-2xl bg-surface-sunk p-6 max-md:order-1 sm:p-8 md:p-10">
        <h3 className="font-display text-xl font-bold tracking-tight text-slate-900 md:text-2xl">
          {REGISTER_PANEL_HEADING}
        </h3>
        <div aria-hidden="true" className="stitch" />
        <p className="text-sm leading-relaxed text-slate-700 md:text-base">
          {REGISTER_GUEST_WORK_NOTICE}
        </p>
      </div>
    </section>
  );
}

interface SentStateProps {
  readonly credentials: Credentials;
  readonly next: string | null;
  readonly onUseDifferentEmail: () => void;
}

/**
 * "Check your email" with the guest-work answer it needs. A component of its own so the guest
 * lists are read only once there is a deadline to warn about — never while the visitor is still
 * typing. `useGuestWorkSummary` is `null` while they load or if either read fails, which reads as
 * "no guest work": the note is a reminder, not a gate.
 */
function SentState({ credentials, next, onUseDifferentEmail }: SentStateProps) {
  const guestWork = useGuestWorkSummary();
  return (
    <CheckYourEmail
      credentials={credentials}
      next={next}
      hasGuestWork={guestWork !== null}
      onUseDifferentEmail={onUseDifferentEmail}
    />
  );
}
