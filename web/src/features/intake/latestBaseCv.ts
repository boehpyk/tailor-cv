import type { BaseCv } from './types';

/**
 * The most recently uploaded `BaseCv`, or `null` for a session with none.
 *
 * Compared by `uploaded_at` rather than array position — the list endpoint's ordering is not part
 * of its contract (technical-plan.md's API contract section says nothing about order), so reading
 * `items[0]` as "latest" would be trusting a guarantee the server never made.
 *
 * **One definition, two readers.** `BaseCvUploadPanel` uses it to decide which CV is on screen;
 * `WorkspacePage` (1.3's `TailorPanel`, before 1.4 folded the launch into the workspace) uses it to
 * decide which CV to tailor. They were a private helper and would have been
 * a copy — and two copies of "which one is newest" drift into a tailoring panel that sends a
 * different CV from the one the user is looking at. Sharing the function makes that disagreement
 * impossible rather than unlikely.
 */
export function latestBaseCv(items: readonly BaseCv[]): BaseCv | null {
  if (items.length === 0) {
    return null;
  }
  return items.reduce((latest, item) => (item.uploaded_at > latest.uploaded_at ? item : latest));
}
