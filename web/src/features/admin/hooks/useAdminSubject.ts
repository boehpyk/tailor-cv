import { useParams } from 'react-router';

import { adminGuestKey, adminUserKey } from './useAdminUser';

import type { AdminSubject } from '@/api/admin';

/**
 * The subject of an admin read page, from its route: `guests/:sessionId/…` is a guest session,
 * `users/:userId/…` an account (slice 4.4, plan §5). One page, two subjects — 2.3's `scopeMap`
 * lesson: the page never builds a prefix, a key or a link from a bare id.
 */
export function useAdminSubject(): AdminSubject {
  // One of the two is always set under these routes; '' only satisfies the type.
  const { userId = '', sessionId } = useParams();
  return sessionId !== undefined ? { kind: 'guest', sessionId } : { kind: 'user', userId };
}

/** The subject's query-key root: a user's is `adminUserKey`, byte for byte (4.3's keys hold). */
export function subjectKey(actorId: string, subject: AdminSubject): readonly string[] {
  return subject.kind === 'user'
    ? adminUserKey(actorId, subject.userId)
    : adminGuestKey(actorId, subject.sessionId);
}
