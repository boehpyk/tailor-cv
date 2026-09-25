/**
 * The saved-CV picker in the workspace's base-CV tab (AC-38, AC-39, AC-45) — a container.
 *
 * Gated on the auth state first: **nothing** while `anonymous` (a guest never sees it) and nothing
 * while `booting` (no flicker); `unavailable` → "Couldn't check your account…" + Retry. Only when
 * `authenticated` does it read the list, and then it renders its query's own states: loading, error
 * + Retry, empty (a link to `/account`), or a `radiogroup` with a `legend` — newest first, the only
 * CV preselected when there is exactly one, `extraction_failed` CVs listed but disabled with their
 * reason — plus **Use this CV** and AC-45's retention statement.
 *
 * The selection is local `useState` holding an **id**, checked against the current `data.items` on
 * every render, never a copy of a list entry. Use this CV is `useCopySavedBaseCv`, guarded against
 * a same-tick double click with `queryClient.isMutating({ mutationKey })` (AC-39).
 *
 * SKELETON (T25): renders nothing. GREEN is T27; placed in the base-CV tab at T28.
 */
export function SavedBaseCvPicker(): React.JSX.Element | null {
  return null;
}
