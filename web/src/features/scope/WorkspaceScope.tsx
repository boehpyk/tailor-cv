import { WorkspaceScopeContext } from './scopeContext';

import type { WorkspaceScope } from './scopeMap';
import type { ReactNode } from 'react';

/**
 * Sets the scope for a subtree. Rendered by a **route** (plan §0.9) — never by a component deciding
 * at render time who is signed in, because the scope of an already-open run must not change when
 * the auth state does (AC-48). See `scopeContext.ts` for why this is context at all.
 */
export function WorkspaceScopeProvider({
  scope,
  children,
}: {
  readonly scope: WorkspaceScope;
  readonly children: ReactNode;
}): React.JSX.Element {
  return <WorkspaceScopeContext value={scope}>{children}</WorkspaceScopeContext>;
}
