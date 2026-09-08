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

/** An ISO timestamp as a short local string ("7 Sep, 10:00") for the "stored until …" line. */
export function formatStoredUntil(expiresAt: string): string {
  const date = new Date(expiresAt);
  return date.toLocaleString(undefined, {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  });
}
