import { useCurrentUser } from '@/features/auth/hooks/useCurrentUser';

import { WorkspaceScopeProvider } from './WorkspaceScope';

import type { ReactNode } from 'react';

export interface AccountScopeProps {
  /**
   * What to render in the account's scope, handed the user id — a render prop rather than a
   * context read in the child, so a component that needs the id (to key its queries) receives it
   * as a typed value and never has to assert that it is inside an account scope.
   */
  readonly children: (userId: string) => ReactNode;
}

/**
 * Provides the **account** scope for a subtree — used by the routes that are the account's
 * (`/history…`) and by the `/` gate once the store says `authenticated` (plan §0.9). Always under
 * `RequireAuth` or the gate: it does not decide *whether* someone is signed in, only *who*, because
 * the user id roots every account cache key (AC-43).
 *
 * The id comes from `/me` (`['auth', 'me']`, seeded by the login or the boot refresh). While it is
 * unknown the scope cannot be built; if `/me` fails, that is an error state with Retry — never an
 * endless loading line (AC-52).
 *
 * SKELETON (T29): the three states are distinguishable stubs; T31 gives them their copy.
 */
export function AccountScope({ children }: AccountScopeProps): React.JSX.Element {
  const currentUser = useCurrentUser();

  if (currentUser.isError) {
    return <p role="alert">AccountScope: /me failed (skeleton)</p>;
  }
  if (currentUser.data === undefined) {
    return <p role="status">AccountScope: loading the account (skeleton)</p>;
  }
  const userId = currentUser.data.id;
  return (
    <WorkspaceScopeProvider scope={{ kind: 'account', userId }}>
      {children(userId)}
    </WorkspaceScopeProvider>
  );
}
