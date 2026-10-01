import { ACCOUNT_API_TARGET, GUEST_API_TARGET } from '@/api/target';
import { authQueryKeyPrefix } from '@/features/auth/hooks/authCache';
import { tailoringRunsQueryKey } from '@/features/tailoring/hooks/useTailoringRuns';

import type { ApiTarget } from '@/api/target';

/**
 * **Whose workspace this is** — the client's port (slice 2.3, technical plan §0.9, ADR-0023).
 *
 * The run page, the editor's autosave, the export bar and the posting panel are the same components
 * for a guest and for a signed-in user; what differs is where they read and write, with which
 * credential, and under which cache key. That difference is *injected*, not copied: the route
 * decides the scope, this module maps it to concrete values, and a hook asks the map. Components
 * never branch on it (except for copy, plan §7).
 *
 * **The scope is decided by the route, never at request time.** A hook that asked "is someone
 * signed in right now?" before each call would send a guest run's autosave to `/api/me/` the moment
 * a user signed in in another tab. The URL owns the scope as it owns the run id (AC-48).
 *
 * The account variant carries the **user id**, because it roots every cache key: two users in one
 * tab (A signs out, B signs in) must never share an entry, and the id is what keeps them apart
 * (AC-43). It is not in any URL — the bearer says who.
 */
export type WorkspaceScope =
  { readonly kind: 'guest' } | { readonly kind: 'account'; readonly userId: string };

/** The one guest scope — the context's default, so an unwrapped tree is today's workspace. */
export const GUEST_SCOPE: WorkspaceScope = { kind: 'guest' };

/**
 * What a scope resolves to: the transport half (`ApiTarget` — paths and credential) plus the
 * client half (query keys and links). Pure data; nothing here fetches.
 */
export interface ScopeMap extends ApiTarget {
  readonly kind: WorkspaceScope['kind'];
  /**
   * Prepended to every per-feature query key. **Empty for a guest**, so a guest key is exactly
   * today's (`['tailoring', 'tailoringRun', id]`, `['export', 'exportJobs', id]`, …) and every
   * existing test, seeding the cache by those literal keys, is unchanged (R-2). For an account it
   * is `['auth', 'account', userId]`: under 2.1's `['auth']` prefix, so logout, account deletion and
   * a cross-tab sign-out — which already `removeQueries(['auth'])` — clear every tailored document
   * from memory with no new code (AC-43).
   */
  readonly keyRoot: readonly string[];
  /**
   * The key a run's writes invalidate so the scope's list of runs re-reads: 1.3's guest run list,
   * or the account's history (every page, and the workspace's latest-run card, live under it).
   * Not `[...keyRoot, 'tailoring', 'tailoringRuns']` for an account, because the account has no such
   * list — its runs *are* its history, read by `GET /api/me/tailoring-runs` as pages.
   */
  readonly runListKey: readonly unknown[];
  /** Where a run lives in the app: `/runs/{id}` for a guest, `/history/{id}` for an account. */
  readonly linkPrefix: '/runs' | '/history';
}

/**
 * Today's values, as one object so the default scope is referentially stable. Exported for the
 * per-feature key builders' default argument: a key built without a map is a guest key, which is
 * what every caller that predates slice 2.3 — its tests included — means.
 */
export const GUEST_SCOPE_MAP: ScopeMap = {
  kind: 'guest',
  ...GUEST_API_TARGET,
  keyRoot: [],
  runListKey: tailoringRunsQueryKey,
  linkPrefix: '/runs',
};

/**
 * **Every root a guest query key starts with** (slice 2.4, technical plan §0.11).
 *
 * An account's keys share one root (`accountKeyRoot`), so "forget this user's data" is one
 * `removeQueries`. A guest's have none: `GUEST_SCOPE_MAP.keyRoot` is empty so that every guest key
 * stayed byte-for-byte what 1.1–1.5 wrote (R-2). The claim is the first thing that must forget a
 * guest's data wholesale — after it, every guest list is about a session that no longer exists — so
 * the roots are written down here, once, next to the map that decided they would have no prefix:
 *
 * - `['intake']` — `['intake', 'baseCvs']`
 * - `['posting']` — `['posting', 'jobPostings']`
 * - `['tailoring']` — `['tailoring', 'tailoringRuns']`, `['tailoring', 'tailoringRun', id]`
 * - `['export']` — `['export', 'exportJobs', runId]`
 *
 * A fifth guest feature with its own first segment must be added here, or a claim leaves its cache
 * behind; a test pins that every guest key builder's first element is in this list (AC-45). Mutation
 * keys share these roots and are untouched by `removeQueries`, which reads the query cache only.
 */
export const GUEST_QUERY_ROOTS: readonly (readonly [string])[] = [
  ['intake'],
  ['posting'],
  ['tailoring'],
  ['export'],
];

/** The account's key root — `['auth', 'account', userId]`, under 2.1's auth prefix (AC-43). */
export function accountKeyRoot(userId: string): readonly string[] {
  return [...authQueryKeyPrefix, 'account', userId];
}

/**
 * Scope → values. **Pure**: the same scope always maps to equal values, and the guest branch
 * returns exactly today's literal paths and keys (and the same object every time).
 */
export function scopeMap(scope: WorkspaceScope): ScopeMap {
  switch (scope.kind) {
    case 'guest':
      return GUEST_SCOPE_MAP;
    case 'account': {
      const keyRoot = accountKeyRoot(scope.userId);
      return {
        kind: 'account',
        ...ACCOUNT_API_TARGET,
        keyRoot,
        runListKey: [...keyRoot, 'history'],
        linkPrefix: '/history',
      };
    }
  }
}

/** The app path of a run, or of one of its documents, in a scope. */
export function runLink(map: ScopeMap, runId: string, document?: string): string {
  const base = `${map.linkPrefix}/${encodeURIComponent(runId)}`;
  return document === undefined ? base : `${base}/${document}`;
}
