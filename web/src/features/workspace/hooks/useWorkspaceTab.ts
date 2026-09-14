/** The two input tabs, as they are spelt in the URL: `?tab=base-cv` / `?tab=job-posting` (AC-23). */
export type WorkspaceTab = 'base-cv' | 'job-posting';

export interface WorkspaceTabState {
  readonly tab: WorkspaceTab;
  readonly setTab: (tab: WorkspaceTab) => void;
}

/**
 * Which input tab is open. The URL is the state (`?tab=`), not a `useState`, so a refresh and the
 * back button both land where the user was; with no parameter the default is derived from whether
 * the session already has a CV (`hasBaseCv`).
 *
 * F5 skeleton: always `'base-cv'`, and the setter does nothing. The search parameter and the derived
 * default arrive in F7 against qa's red tests.
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- skeleton: read in F7
export function useWorkspaceTab(hasBaseCv: boolean): WorkspaceTabState {
  return { tab: 'base-cv', setTab: () => undefined };
}
