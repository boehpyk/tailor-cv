import { useLocation, useNavigate } from 'react-router';

import { useAuth } from '@/features/auth/hooks/useAuth';
import { ACCOUNT_RUN_LINKS, runLink } from '@/features/scope/scopeMap';
import { useScopeMap } from '@/features/scope/useWorkspaceScope';

import { GuestWorkOffer } from './GuestWorkOffer';
import { RegistrationCta } from './RegistrationCta';

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
 *   `navigate('/history/:runId/:document', { replace: true })` — `replace`, because the guest
 *   entry it leaves names a session that no longer exists, so "back" must not return to it.
 *   (While `/me` has not answered who is signed in, there is no user id to key the claim, so
 *   nothing — the same as booting.)
 */
export function GuestRunPrompt({
  runId,
  document,
  runStatus,
}: GuestRunPromptProps): React.JSX.Element | null {
  const map = useScopeMap();
  const auth = useAuth();
  const location = useLocation();
  const navigate = useNavigate();

  if (map.kind !== 'guest') {
    return null;
  }
  switch (auth.status) {
    case 'booting':
    case 'unavailable':
      return null;
    case 'anonymous':
      return runStatus === 'succeeded' ? <RegistrationCta next={location.pathname} /> : null;
    case 'authenticated': {
      if (auth.user === undefined) {
        return null;
      }
      return (
        <GuestWorkOffer
          key={auth.user.id}
          userId={auth.user.id}
          onClaimed={() => {
            void navigate(runLink(ACCOUNT_RUN_LINKS, runId, document), { replace: true });
          }}
        />
      );
    }
  }
}
