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
      className="space-y-2 rounded-md border border-sky-200 bg-sky-50 px-4 py-3 text-sm"
    >
      <p className="font-medium text-sky-950">{CTA_SENTENCE}</p>
      <p className="text-sky-900">{CTA_RETENTION_LINE}</p>
      <div className="flex flex-wrap items-center gap-3 pt-1">
        <Link
          to={`/register${search}`}
          className="rounded-md bg-slate-900 px-3 py-1.5 font-medium text-white"
        >
          {CTA_REGISTER_LABEL}
        </Link>
        <Link
          to={`/login${search}`}
          className="font-medium text-slate-900 underline underline-offset-2"
        >
          {CTA_LOGIN_LABEL}
        </Link>
        <button
          type="button"
          onClick={() => {
            setDismissed(true);
          }}
          className="ml-auto text-slate-600 underline underline-offset-2"
        >
          {NOT_NOW_LABEL}
        </button>
      </div>
    </section>
  );
}
