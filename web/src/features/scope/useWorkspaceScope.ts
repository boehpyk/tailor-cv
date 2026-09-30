import { useContext, useMemo } from 'react';

import { WorkspaceScopeContext } from './scopeContext';
import { GUEST_SCOPE, scopeMap } from './scopeMap';

import type { ScopeMap, WorkspaceScope } from './scopeMap';

/** The scope of the subtree this component renders in — guest unless a route said otherwise. */
export function useWorkspaceScope(): WorkspaceScope {
  return useContext(WorkspaceScopeContext);
}

/**
 * The scope's paths, credential, key root and link prefix. Memoised on the scope's *identity*
 * (`kind` and `userId`), so a provider re-rendered with an equal-but-new object does not hand every
 * hook a new map — keys and callbacks built from it stay stable.
 */
export function useScopeMap(): ScopeMap {
  const scope = useWorkspaceScope();
  const userId = scope.kind === 'account' ? scope.userId : null;
  return useMemo(
    () => scopeMap(userId === null ? GUEST_SCOPE : { kind: 'account', userId }),
    [userId],
  );
}
