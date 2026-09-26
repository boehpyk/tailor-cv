/**
 * A one-off notice carried to `/` in React Router's location state — today only AC-40's "Your
 * account and saved CVs were deleted.", which has to outlive the page that deleted the account.
 *
 * **The reader only ever renders a notice it knows.** History state is same-origin, so nobody else
 * can write it, but it survives reloads and outlives releases; a reader that rendered whatever
 * string it found would be one refactor away from rendering something nobody meant as copy. So the
 * state carries the sentence (a test reads it there), and the reader checks it against the list of
 * sentences this module is allowed to show.
 */
import { ACCOUNT_DELETED_NOTICE } from '@/features/savedCvs/savedCvsCopy';

const KNOWN_NOTICES: readonly string[] = [ACCOUNT_DELETED_NOTICE];

export interface HomeNoticeState {
  readonly notice: string;
}

export function homeNoticeState(notice: string): HomeNoticeState {
  return { notice };
}

/** The notice in `state`, if it is one this app sends; `null` for anything else. */
export function homeNoticeFrom(state: unknown): string | null {
  if (typeof state !== 'object' || state === null || !('notice' in state)) {
    return null;
  }
  const notice: unknown = (state as { readonly notice: unknown }).notice;
  return typeof notice === 'string' && KNOWN_NOTICES.includes(notice) ? notice : null;
}
