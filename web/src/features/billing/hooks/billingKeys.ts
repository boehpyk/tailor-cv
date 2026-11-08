import { accountKeyRoot } from '@/features/scope/scopeMap';

/**
 * The credits read's key: `[...accountKeyRoot(userId), 'billing', 'credits']` (plan §5). Under the
 * account root, so a sign-out's `removeQueries(['auth'])` drops it, and no guest key exists:
 * `GUEST_QUERY_ROOTS` is unchanged. Its own module so the tailoring hooks that invalidate it import
 * no billing hook (and no cycle).
 */
export function creditsQueryKey(userId: string): readonly unknown[] {
  return [...accountKeyRoot(userId), 'billing', 'credits'];
}
