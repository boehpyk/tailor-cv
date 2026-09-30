import { useCurrentUser } from '@/features/auth/hooks/useCurrentUser';

import { WorkspaceScopeProvider } from './WorkspaceScope';

/** While `/me` answers who is signed in — normally never seen: login and the boot seed it. */
const ACCOUNT_LOADING_NOTE = 'Loading your account…';
/** AC-52: `/me` failed. An error with Retry, never an endless loading line. */
const ACCOUNT_LOAD_ERROR_NOTE = "Couldn't load your account.";
const ACCOUNT_RETRY_LABEL = 'Retry';

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
 * The Retry re-asks `/me` (`refetch`), which is the one thing that can change the answer.
 */
export function AccountScope({ children }: AccountScopeProps): React.JSX.Element {
  const currentUser = useCurrentUser();

  if (currentUser.data === undefined) {
    if (currentUser.isError) {
      return (
        <div role="alert" className="mb-10 space-y-3">
          <p className="text-slate-700">{ACCOUNT_LOAD_ERROR_NOTE}</p>
          <button
            type="button"
            onClick={() => {
              void currentUser.refetch();
            }}
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white"
          >
            {ACCOUNT_RETRY_LABEL}
          </button>
        </div>
      );
    }
    return (
      <p role="status" className="mb-10 text-slate-600">
        {ACCOUNT_LOADING_NOTE}
      </p>
    );
  }
  const userId = currentUser.data.id;
  return (
    <WorkspaceScopeProvider scope={{ kind: 'account', userId }}>
      {children(userId)}
    </WorkspaceScopeProvider>
  );
}
