import { useId, useRef } from 'react';

import type { WorkspaceTab } from '../hooks/useWorkspaceTab';
import type { KeyboardEvent, ReactNode } from 'react';

export interface InputTabsProps {
  /**
   * The open tab, or `null` while the workspace cannot know which one to open yet — the lists are
   * still loading, so the derived default (`useWorkspaceTab`) has nothing to derive from. In that
   * state no tablist is drawn and both panels are shown stacked: a tab strip that would switch its
   * selection the moment the data lands reads as a flicker, not as loading.
   */
  readonly tab: WorkspaceTab | null;
  readonly onTabChange: (tab: WorkspaceTab) => void;
  /** The base-CV panel — mounted whichever tab is open, so an upload in flight is never unmounted. */
  readonly baseCvPanel: ReactNode;
  /** The job-posting panel — mounted whichever tab is open, so a half-typed posting survives. */
  readonly jobPostingPanel: ReactNode;
}

/** Display order — which is also the arrow-key order. */
const TAB_ORDER: readonly WorkspaceTab[] = ['base-cv', 'job-posting'];

const TAB_LABELS: Readonly<Record<WorkspaceTab, string>> = {
  'base-cv': 'Base CV',
  'job-posting': 'Job posting',
};

/**
 * Where an arrow or Home/End key moves the selection from `current`, or `null` for any other key.
 * Wraps at both ends, which is what the ARIA tabs pattern asks for and what a user pressing → on
 * the last tab expects: to get somewhere, not to be ignored.
 */
function tabAfterKey(current: WorkspaceTab, key: string): WorkspaceTab | null {
  const index = TAB_ORDER.indexOf(current);
  const last = TAB_ORDER.length - 1;
  switch (key) {
    case 'ArrowRight':
      return TAB_ORDER[index === last ? 0 : index + 1] ?? null;
    case 'ArrowLeft':
      return TAB_ORDER[index === 0 ? last : index - 1] ?? null;
    case 'Home':
      return TAB_ORDER[0] ?? null;
    case 'End':
      return TAB_ORDER[last] ?? null;
    default:
      return null;
  }
}

/**
 * The two input tabs — presentational (AC-23). `role="tablist"`, two `role="tab"`s with
 * `aria-selected` and `aria-controls`, two `role="tabpanel"`s **both mounted**, the inactive one
 * `hidden`.
 *
 * **Both panels stay mounted** because each holds user input that is nowhere else — a half-typed
 * posting in `JobPostingPanel`'s own state, an upload in flight in `BaseCvUploadPanel`'s mutation.
 * Unmounting the inactive panel would throw that away on every tab switch; the `hidden` attribute
 * takes it out of the layout and the accessibility tree while keeping the component, and its
 * state, alive.
 *
 * **Keyboard.** The ARIA tabs pattern with automatic activation: the selected tab is the only one
 * in the Tab sequence (`tabIndex` 0 / −1, "roving"), and ←/→/Home/End move both the selection and
 * the focus. Focus is moved imperatively from the key handler rather than in an effect that watches
 * `tab`: the button to focus already exists (both tabs are always rendered), and an effect would
 * also steal focus when the URL changed for any *other* reason — the back button, a link — which
 * is not what a user pressing "back" wants.
 */
export function InputTabs({
  tab,
  onTabChange,
  baseCvPanel,
  jobPostingPanel,
}: InputTabsProps): React.JSX.Element {
  const baseCvTabId = useId();
  const baseCvPanelId = useId();
  const jobPostingTabId = useId();
  const jobPostingPanelId = useId();
  const tabRefs = useRef<Record<WorkspaceTab, HTMLButtonElement | null>>({
    'base-cv': null,
    'job-posting': null,
  });

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, from: WorkspaceTab): void {
    const next = tabAfterKey(from, event.key);
    if (next === null) {
      return;
    }
    event.preventDefault();
    onTabChange(next);
    tabRefs.current[next]?.focus();
  }

  const ids: Readonly<Record<WorkspaceTab, { tab: string; panel: string }>> = {
    'base-cv': { tab: baseCvTabId, panel: baseCvPanelId },
    'job-posting': { tab: jobPostingTabId, panel: jobPostingPanelId },
  };

  // One tree for both the loading and the settled shape, with the tablist as a conditional first
  // slot: `false` still occupies a position in React's reconciliation, so the two panel wrappers
  // keep their fibers — and the panels their state and their query observers — when the strip
  // appears. Two `return`s with different sibling layouts would remount both panels and refetch
  // both lists the moment they had loaded.
  return (
    <div>
      {tab !== null && (
        <div
          role="tablist"
          aria-label="Your inputs"
          className="mb-6 flex gap-1 border-b border-slate-200"
        >
          {TAB_ORDER.map((candidate) => {
            const selected = candidate === tab;
            return (
              <button
                key={candidate}
                ref={(element) => {
                  tabRefs.current[candidate] = element;
                }}
                type="button"
                role="tab"
                id={ids[candidate].tab}
                aria-controls={ids[candidate].panel}
                aria-selected={selected}
                tabIndex={selected ? 0 : -1}
                onClick={() => {
                  onTabChange(candidate);
                }}
                onKeyDown={(event) => {
                  handleKeyDown(event, candidate);
                }}
                className={
                  selected
                    ? '-mb-px border-b-2 border-slate-900 px-3 py-2 text-sm font-medium text-slate-900'
                    : '-mb-px border-b-2 border-transparent px-3 py-2 text-sm font-medium text-slate-500 hover:text-slate-800'
                }
              >
                {TAB_LABELS[candidate]}
              </button>
            );
          })}
        </div>
      )}

      <div
        role={tab === null ? undefined : 'tabpanel'}
        id={baseCvPanelId}
        aria-labelledby={tab === null ? undefined : baseCvTabId}
        hidden={tab !== null && tab !== 'base-cv'}
      >
        {baseCvPanel}
      </div>
      <div
        role={tab === null ? undefined : 'tabpanel'}
        id={jobPostingPanelId}
        aria-labelledby={tab === null ? undefined : jobPostingTabId}
        hidden={tab !== null && tab !== 'job-posting'}
      >
        {jobPostingPanel}
      </div>
    </div>
  );
}
