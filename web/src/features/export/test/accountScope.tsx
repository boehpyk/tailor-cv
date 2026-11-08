import { WorkspaceScopeProvider } from '@/features/scope/WorkspaceScope';
import { scopeMap } from '@/features/scope/scopeMap';

import type { ReactNode } from 'react';

/**
 * Slice 5.2 (AC-15, AC-16, AC-36): exports are an account's, so the export bar is mounted in
 * account scope in every test. Pair it with `signInAs(USER_A)` (the bearer) and `stubExportFetch`
 * (which answers the `/api/me/…` paths).
 */
const ACCOUNT = { kind: 'account', userId: 'user-a' } as const;

export const ACCOUNT_SCOPE_MAP = scopeMap(ACCOUNT);

export function InAccountScope({ children }: { readonly children: ReactNode }): React.JSX.Element {
  return <WorkspaceScopeProvider scope={ACCOUNT}>{children}</WorkspaceScopeProvider>;
}
