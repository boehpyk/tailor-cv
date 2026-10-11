/**
 * The list's one-off notice after an erase (AC-35), carried to `/admin` in the router's location
 * state by `AccountActions`. The reader checks the shape, because history state outlives releases.
 */
export interface ErasedNotice {
  readonly email: string;
  readonly unlinkFailures: number;
}

export function erasedNoticeState(notice: ErasedNotice): { readonly erased: ErasedNotice } {
  return { erased: notice };
}

export function erasedNoticeFrom(state: unknown): ErasedNotice | null {
  if (typeof state !== 'object' || state === null || !('erased' in state)) return null;
  const erased: unknown = (state as { readonly erased: unknown }).erased;
  if (typeof erased !== 'object' || erased === null) return null;
  const { email, unlinkFailures } = erased as Record<string, unknown>;
  return typeof email === 'string' && typeof unlinkFailures === 'number'
    ? { email, unlinkFailures }
    : null;
}
