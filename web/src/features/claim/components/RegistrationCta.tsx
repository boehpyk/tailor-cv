/* eslint-disable @typescript-eslint/no-unused-vars -- T29 SKELETON: the parameters are the signature qa's T30 tests compile against; T31 uses them and deletes this line. */
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
 *
 * SKELETON (T29): renders nothing; T31 builds it.
 */
export function RegistrationCta(_props: RegistrationCtaProps): React.JSX.Element | null {
  return null;
}
