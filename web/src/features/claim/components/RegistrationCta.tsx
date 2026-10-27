import { useState } from 'react';
import { Link } from 'react-router';

import {
  CTA_LOGIN_LABEL,
  CTA_REGION_LABEL,
  CTA_REGISTER_LABEL,
  CTA_RETENTION_LINE,
  CTA_SENTENCE,
  NOT_NOW_LABEL,
} from '../claimCopy';

export interface RegistrationCtaProps {
  /**
   * The app path to come back to after registering or signing in — the guest run page's
   * `location.pathname`, e.g. `/runs/R/cv`. Encoded into both links as `?next=` (AC-35, AC-36);
   * 2.1's `safeNext` judges it on the way back.
   */
  readonly next: string;
}

/**
 * The registration CTA (AC-35, PRD §6) — **presentational**: one `role="region"` labelled
 * *"Save your work"* with the PRD sentence, the 24-hour retention line, **Create an account** →
 * `/register?next=…`, **Sign in** → `/login?next=…`, and **Not now**, which hides it for this page's
 * lifetime (`useState`, never browser storage).
 *
 * *When* it shows (anonymous, guest scope, a `succeeded` run) is `GuestRunPrompt`'s decision, not
 * this component's.
 */
export function RegistrationCta({ next }: RegistrationCtaProps): React.JSX.Element | null {
  const [dismissed, setDismissed] = useState(false);
  if (dismissed) {
    return null;
  }
  const search = `?next=${encodeURIComponent(next)}`;

  return (
    <section
      aria-label={CTA_REGION_LABEL}
      className="space-y-2 rounded-xl border border-slate-200 bg-brand-50 px-4 py-4 text-sm sm:px-5"
    >
      <p className="font-semibold text-slate-900">{CTA_SENTENCE}</p>
      <p className="text-slate-700">{CTA_RETENTION_LINE}</p>
      <div className="flex flex-wrap items-center gap-3 pt-1">
        <Link
          to={`/register${search}`}
          className="inline-flex items-center min-h-11 rounded-lg bg-brand-600 px-4 text-sm font-semibold text-white hover:bg-brand-700 disabled:opacity-60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 sm:min-h-9"
        >
          {CTA_REGISTER_LABEL}
        </Link>
        <Link
          to={`/login${search}`}
          className="rounded-sm font-semibold text-brand-700 underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600"
        >
          {CTA_LOGIN_LABEL}
        </Link>
        <button
          type="button"
          onClick={() => {
            setDismissed(true);
          }}
          className="ml-auto min-h-11 rounded-sm px-1 font-medium text-slate-700 underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 sm:min-h-0"
        >
          {NOT_NOW_LABEL}
        </button>
      </div>
    </section>
  );
}
