/**
 * A callback ref that moves focus to its element when the element is attached — the "focus moves to
 * the outcome" half of a form's success or failure (slice 2.5, AC-52).
 *
 * **A module-level function, not a hook.** React calls a callback ref when its identity changes, so
 * a function defined once here is called exactly once per mount: the outcome is focused when it
 * appears and never stolen back on a later re-render. No effect is needed — attaching the node *is*
 * the moment, and React hands it over directly.
 *
 * The element must be focusable: give a non-interactive one `tabIndex={-1}` (focusable by script,
 * not in the tab order).
 */
export function focusOnMount(element: HTMLElement | null): void {
  element?.focus();
}
