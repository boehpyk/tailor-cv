import { createContext } from 'react';

import { GUEST_SCOPE } from './scopeMap';

import type { WorkspaceScope } from './scopeMap';

/**
 * The scope, as React context — **provided by the route** (`/runs/…` and the anonymous `/` → guest;
 * `/history/…` and the authenticated `/` → account), read through `useWorkspaceScope` /
 * `useScopeMap`.
 *
 * Context rather than a prop, and this is the one justification the React conventions ask for: the
 * scope is read four components deep (run page → editor → autosave hook; run page → export bar →
 * its hooks), by hooks that must not grow a parameter every caller threads through. It is not
 * *global* state — it is a fact about a subtree, set once by the route that renders it, and it never
 * changes while that subtree is mounted.
 *
 * **The default is guest**, so every tree that never mentions a scope — every existing test, and
 * every guest route — renders exactly as it did before slice 2.3 (R-2).
 *
 * In its own module so the provider file exports only a component (fast refresh) and the hooks
 * file only hooks.
 */
export const WorkspaceScopeContext = createContext<WorkspaceScope>(GUEST_SCOPE);
