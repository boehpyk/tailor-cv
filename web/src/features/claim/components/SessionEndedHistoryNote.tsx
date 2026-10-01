import { Link } from 'react-router';

import { useAuth } from '@/features/auth/hooks/useAuth';
import { ACCOUNT_RUN_LINKS, runLink } from '@/features/scope/scopeMap';

import { SESSION_ENDED_HISTORY_LINK_LABEL, SESSION_ENDED_HISTORY_NOTE } from '../claimCopy';

import type { TailoredDocumentKind } from '@/features/tailoring/types';

export interface SessionEndedHistoryNoteProps {
  /** The guest run whose session has ended — the id in `/runs/:runId/:document`. */
  readonly runId: string;
  /** The document segment on screen, so the link lands on the same tab in the history. */
  readonly document: TailoredDocumentKind;
}

/**
 * AC-41: appended to 1.4's `guest_session_expired` copy on a guest run page, **for a signed-in
 * visitor only**. Their session may have ended because they kept the work — a claim deletes the
 * guest session — so the run may be in their history under the same id. An anonymous visitor sees
 * 1.4's copy unchanged (this renders nothing), because for them there is no history to point at.
 *
 * The link is built by `runLink` from the account's prefix, never by hand (2.3's rule): it needs no
 * user id, so it renders even before `/me` has answered.
 */
export function SessionEndedHistoryNote({
  runId,
  document,
}: SessionEndedHistoryNoteProps): React.JSX.Element | null {
  const auth = useAuth();
  if (auth.status !== 'authenticated') {
    return null;
  }
  return (
    <p className="text-sm text-slate-700">
      <span>{SESSION_ENDED_HISTORY_NOTE}</span>{' '}
      <Link
        to={runLink(ACCOUNT_RUN_LINKS, runId, document)}
        className="font-medium text-slate-900 underline underline-offset-2"
      >
        {SESSION_ENDED_HISTORY_LINK_LABEL}
      </Link>
    </p>
  );
}
