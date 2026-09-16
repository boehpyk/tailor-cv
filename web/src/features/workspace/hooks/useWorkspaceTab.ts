import { useCallback } from 'react';
import { useSearchParams } from 'react-router';

/** The two input tabs, as they are spelt in the URL: `?tab=base-cv` / `?tab=job-posting` (AC-23). */
export type WorkspaceTab = 'base-cv' | 'job-posting';

export interface WorkspaceTabState {
  readonly tab: WorkspaceTab;
  readonly setTab: (tab: WorkspaceTab) => void;
}

/** The parameter's name, in one place. */
const TAB_PARAM = 'tab';

/**
 * Narrow a URL value to a tab. A `?tab=` somebody typed or mangled is neither an error nor a crash:
 * it simply does not choose, and the derived default applies as if it were absent.
 */
function parseTab(value: string | null): WorkspaceTab | null {
  return value === 'base-cv' || value === 'job-posting' ? value : null;
}

/**
 * Which input tab is open. **The URL is the state** (`?tab=`), not a `useState`: a refresh and the
 * back button both land where the user was, and a link can open the workspace on a given tab.
 *
 * With no parameter the tab is **derived**, during render, from whether the session already has a
 * CV (`hasBaseCv`): a first-time visitor starts on *Base CV*, and one who has uploaded already is
 * taken to the next thing to do. The derivation is not written back into the URL — a `useEffect`
 * that "normalised" the address would push a history entry the user never asked for, and the
 * absent parameter already means "the sensible default" unambiguously.
 *
 * `setTab` pushes a history entry rather than replacing one. Each tab is a place the user went,
 * so the back button retraces the visit; `replace` would make "back" skip every tab switch at once.
 * Other parameters in the URL are carried across untouched.
 */
export function useWorkspaceTab(hasBaseCv: boolean): WorkspaceTabState {
  const [searchParams, setSearchParams] = useSearchParams();

  const tab = parseTab(searchParams.get(TAB_PARAM)) ?? (hasBaseCv ? 'job-posting' : 'base-cv');

  const setTab = useCallback(
    (next: WorkspaceTab) => {
      setSearchParams((current) => {
        const updated = new URLSearchParams(current);
        updated.set(TAB_PARAM, next);
        return updated;
      });
    },
    [setSearchParams],
  );

  return { tab, setTab };
}
