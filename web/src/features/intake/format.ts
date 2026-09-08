/**
 * Small, pure formatters shared by the intake feature's presentational components.
 *
 * Split out of `BaseCvUploadPanel.tsx` (T35) so `BaseCvCard.tsx` can use them without importing
 * from the container — a presentational component formats what it is given, it does not reach
 * into the component that owns the state.
 */

/** A byte count as a short human string ("512 B", "2 KB", "1.4 MB"). */
export function formatSize(bytes: number): string {
  if (bytes < 1024) {
    return `${String(bytes)} B`;
  }
  const kib = bytes / 1024;
  if (kib < 1024) {
    return `${kib.toFixed(0)} KB`;
  }
  return `${(kib / 1024).toFixed(1)} MB`;
}

/**
 * An ISO timestamp as the short local string the plan specifies for "stored until …":
 * day, short month, 24-hour clock — "8 Sep 10:00".
 *
 * Assembled from `formatToParts` rather than handed to `toLocaleString` with a partial options
 * bag, and the locale is pinned rather than left as `undefined`. Both are deliberate:
 *
 * `toLocaleString(undefined, …)` means "whatever this browser decides", which produced
 * "Sep 8, 10:00 AM" on an en-US default — month-first, comma, 12-hour — so the same page read
 * differently for different visitors and no test could pin it. That is a poor trade for a line
 * whose whole job is to make a retention promise legible: a user checking whether their CV is
 * still there should not have to parse a different format than the person next to them.
 *
 * This is not a decision against localization. Every other string in this feature is hardcoded
 * English, so a date that localizes while the sentence around it does not is inconsistent in the
 * one direction that also breaks the tests. When the product grows a locale, this function is the
 * single place that changes.
 */
export function formatStoredUntil(expiresAt: string): string {
  // 'en-US' supplies only the TOKEN SPELLINGS here, not the order: the parts are reassembled
  // below, so the locale's own day/month sequence is irrelevant. It is picked over 'en-GB'
  // because en-GB abbreviates September as "Sept", and the plan's example says "8 Sep 10:00".
  const parts = new Intl.DateTimeFormat('en-US', {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(new Date(expiresAt));

  const find = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find((part) => part.type === type)?.value ?? '';

  return `${find('day')} ${find('month')} ${find('hour')}:${find('minute')}`;
}
