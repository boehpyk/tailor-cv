import type { ReactNode } from 'react';

export interface RequireAuthProps {
  /** What to render once `useAuth()` says `authenticated`. */
  readonly children: ReactNode;
}

/**
 * The route guard (AC-41): `booting` → loading; `anonymous` → `Navigate` to
 * `/login?next=<this path>`; `unavailable` → an error with **Retry**, never a redirect;
 * `authenticated` → `children`. **T40 SKELETON** — renders nothing.
 */
export function RequireAuth({ children }: RequireAuthProps) {
  void children;
  return null;
}
