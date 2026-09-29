import { AccountDeletedNotice } from '@/features/auth/components/AccountDeletedNotice';
import { useAuth } from '@/features/auth/hooks/useAuth';
import { AccountScope } from '@/features/scope/AccountScope';

import { AccountWorkspace } from './AccountWorkspace';
import { GuestWorkspace } from './GuestWorkspace';

/**
 * The `/` route — **the workspace follows the auth state** (slice 2.3, AC-38, ADR-0023):
 *
 * | Auth state      | Renders                                                               |
 * |-----------------|-----------------------------------------------------------------------|
 * | `booting`       | one `role="status"` line, and **nothing typeable** (H-56)             |
 * | `unavailable`   | "Couldn't check whether you're signed in" + Retry — neither workspace |
 * | `anonymous`     | `GuestWorkspace` — 1.4–2.2's workspace, unchanged, in the guest scope |
 * | `authenticated` | `AccountWorkspace`, inside `AccountScope` (the account's key root)    |
 *
 * **Why nothing is mounted while `booting`.** 2.2's `/verify` watched typing into `/register` wiped
 * by the boot refresh: one tree while booting, another once settled, so React unmounted the input.
 * Here the gate avoids that the other way round — nothing a user can type into exists until the
 * question "who is this?" has an answer, so no answer can throw typing away (plan §7).
 *
 * **Why neither workspace while `unavailable`.** Showing the guest one would start a guest session
 * and put a signed-in user's next run somewhere their history will never see (OQ-10).
 *
 * The account-deleted notice (2.2's AC-40) sits above every state: it is about what just happened,
 * not about who is signed in now.
 *
 * SKELETON (T29): the four branches are real; `booting` and `unavailable` render distinguishable
 * stubs, and T31 gives them their copy.
 */
export function WorkspacePage(): React.JSX.Element {
  return (
    <>
      <AccountDeletedNotice />
      <WorkspaceGate />
    </>
  );
}

function WorkspaceGate(): React.JSX.Element {
  const auth = useAuth();

  switch (auth.status) {
    case 'booting':
      return <p role="status">WorkspacePage: booting (skeleton)</p>;
    case 'unavailable':
      return <p role="alert">WorkspacePage: unavailable (skeleton)</p>;
    case 'anonymous':
      return <GuestWorkspace />;
    case 'authenticated':
      return <AccountScope>{(userId) => <AccountWorkspace userId={userId} />}</AccountScope>;
  }
}
