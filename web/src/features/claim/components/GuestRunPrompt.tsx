/* eslint-disable @typescript-eslint/no-unused-vars -- T29 SKELETON: the parameters are the signature qa's T30 tests compile against; T31 uses them and deletes this line. */
import type { TailoredDocumentKind, TailoringRunStatus } from '@/features/tailoring/types';

export interface GuestRunPromptProps {
  /** The run on screen — the id in `/runs/:runId/:document`. */
  readonly runId: string;
  /** The document segment on screen, so success lands on the same tab in the history. */
  readonly document: TailoredDocumentKind;
  /** The run's status once known; `null` while it loads or cannot be read. */
  readonly runStatus: TailoringRunStatus | null;
}

/**
 * The one slot slice 2.4 adds to a run page (technical plan §7). `RunPage` renders it
 * unconditionally; this component owns every branch, so the page does not branch on scope:
 *
 * - **account scope** → nothing (the run is already the account's).
 * - guest scope, auth **`booting` / `unavailable`** → nothing (C-43: "who is this?" has no answer).
 * - guest scope, **`anonymous`**, `runStatus === 'succeeded'` → `RegistrationCta` with
 *   `next` = this page's path (C-34); any other status → nothing (C-35).
 * - guest scope, **`authenticated`** → `GuestWorkOffer` (C-36), and on success
 *   `navigate('/history/:runId/:document', { replace: true })`.
 *
 * SKELETON (T29): renders nothing; T31 builds it.
 */
export function GuestRunPrompt(_props: GuestRunPromptProps): React.JSX.Element | null {
  return null;
}
