/**
 * The account page's one-off notice after a content delete (AC-29), carried to
 * `/admin/users/:userId` in the router's location state by `DeleteContentButton`. The sentence is
 * built by the sender; the reader checks the shape, because history state outlives releases.
 */
export interface ContentNotice {
  readonly text: string;
  readonly unlinkFailures: number;
}

export function contentNoticeState(notice: ContentNotice): {
  readonly contentNotice: ContentNotice;
} {
  return { contentNotice: notice };
}

export function contentNoticeFrom(state: unknown): ContentNotice | null {
  if (typeof state !== 'object' || state === null || !('contentNotice' in state)) return null;
  const notice: unknown = (state as { readonly contentNotice: unknown }).contentNotice;
  if (typeof notice !== 'object' || notice === null) return null;
  const { text, unlinkFailures } = notice as Record<string, unknown>;
  return typeof text === 'string' && typeof unlinkFailures === 'number'
    ? { text, unlinkFailures }
    : null;
}
