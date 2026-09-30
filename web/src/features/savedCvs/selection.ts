import type { SavedBaseCv } from './types';

/**
 * Which saved CV is selected: the user's choice if it is still in the list and usable
 * (`extracted`), else the only CV when there is exactly one and it is usable, else none (2.2's
 * AC-38, 2.3's AC-39 / OQ-14).
 *
 * Derived on every render from the id the owner keeps in `useState` and the query's `items` — so a
 * CV deleted in another tab can never stay selected, and nothing copies the list into state. Shared
 * by the picker (which draws the selection) and the account workspace (which launches with it), so
 * the two can never disagree about which CV a run will use.
 */
export function effectiveSelection(
  items: readonly SavedBaseCv[],
  chosenId: string | null,
): string | null {
  const usable = items.filter((cv) => cv.status === 'extracted');
  if (chosenId !== null && usable.some((cv) => cv.id === chosenId)) {
    return chosenId;
  }
  const [only] = items;
  return items.length === 1 && only?.status === 'extracted' ? only.id : null;
}
