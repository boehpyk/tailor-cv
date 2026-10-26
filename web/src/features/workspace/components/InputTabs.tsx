import { useId, useRef } from 'react';

import {
  STEP_RAIL_BASE_CV_CAPTION,
  STEP_RAIL_JOB_POSTING_CAPTION,
  STEP_RAIL_TAILOR_CAPTION,
  STEP_RAIL_TAILOR_LABEL,
  stepOfThree,
} from '../workspaceCopy';

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
  /**
   * What follows the panels in the panel column — the workspace's tailoring section — so it lines
   * up with the panels beside the rail rather than running under it.
   */
  readonly children?: ReactNode;
}

/** Display order — which is also the arrow-key order. */
const TAB_ORDER: readonly WorkspaceTab[] = ['base-cv', 'job-posting'];

const TAB_LABELS: Readonly<Record<WorkspaceTab, string>> = {
  'base-cv': 'Base CV',
  'job-posting': 'Job posting',
};

/** C-4: decoration under each tab's name, hidden from its accessible name. */
const TAB_CAPTIONS: Readonly<Record<WorkspaceTab, string>> = {
  'base-cv': STEP_RAIL_BASE_CV_CAPTION,
  'job-posting': STEP_RAIL_JOB_POSTING_CAPTION,
};

/** The rail's numbered badge. `size-7` at every state so a selection never shifts the label. */
const BADGE_CLASS =
  'flex size-7 flex-none items-center justify-center rounded-full text-sm font-semibold max-sm:hidden';

/**
 * Where an arrow or Home/End key moves the selection from `current`, or `null` for any other key.
 * Wraps at both ends, which is what the ARIA tabs pattern asks for and what a user pressing → on
 * the last tab expects: to get somewhere, not to be ignored. ↓/↑ act as →/← (AC-13): the strip is
 * a vertical rail at `sm`+ and horizontal below, and `aria-orientation` cannot follow a CSS
 * breakpoint, so both arrow pairs work at every width.
 */
function tabAfterKey(current: WorkspaceTab, key: string): WorkspaceTab | null {
  const index = TAB_ORDER.indexOf(current);
  const last = TAB_ORDER.length - 1;
  switch (key) {
    case 'ArrowRight':
    case 'ArrowDown':
      return TAB_ORDER[index === last ? 0 : index + 1] ?? null;
    case 'ArrowLeft':
    case 'ArrowUp':
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
  children,
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

  // One tree for both the loading and the settled shape, with the rail's contents conditional
  // inside a column that is always there: the two panel wrappers keep their position, so their
  // fibers — and the panels' state and query observers — survive the strip appearing. Two
  // `return`s with different sibling layouts would remount both panels and refetch both lists the
  // moment they had loaded. The rail column also keeps its width while empty, so nothing jumps.
  return (
    // At `sm`+ a two-column grid: the step rail, then the panel column (web-design-refresh AC-14).
    // Below `sm` one column: *Step n of 3*, the same two tabs side by side, the panels.
    <div className="sm:grid sm:grid-cols-[14rem_minmax(0,1fr)] sm:items-start sm:gap-x-12">
      <div className="mb-6 sm:mb-0">
        {tab !== null && (
          <>
            {/* Derived during render from the open tab — no state of its own. */}
            <p className="mb-3 text-sm font-semibold text-slate-600 sm:hidden">
              {stepOfThree(tab === 'base-cv' ? 1 : 2)}
            </p>
            <div
              role="tablist"
              aria-label="Your inputs"
              className="grid grid-cols-2 gap-1 rounded-full bg-surface-sunk p-1 sm:flex sm:flex-col sm:gap-1.5 sm:rounded-none sm:bg-transparent sm:p-0"
            >
              {TAB_ORDER.map((candidate, index) => {
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
                    className={`flex min-h-11 items-center justify-center gap-3.5 rounded-full px-3 text-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 sm:justify-start sm:rounded-lg sm:border sm:px-4 sm:py-3.5 sm:text-left ${
                      selected
                        ? 'bg-white text-slate-900 shadow-sm sm:border-slate-200'
                        : 'text-slate-600 hover:text-slate-900 sm:border-transparent'
                    }`}
                  >
                    <span
                      aria-hidden="true"
                      className={`${BADGE_CLASS} ${
                        selected
                          ? 'bg-slate-900 text-white'
                          : 'border border-slate-300 text-slate-600'
                      }`}
                    >
                      {index + 1}
                    </span>
                    <span className="flex flex-col">
                      <span className="font-semibold">{TAB_LABELS[candidate]}</span>
                      <span aria-hidden="true" className="text-xs text-slate-500 max-sm:hidden">
                        {TAB_CAPTIONS[candidate]}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
            {/* OQ-21: the third step is not a tab — a tablist may own only tabs, and tailoring is
                a section of this page, not a panel the tablist controls. On the guest workspace
                the stage is always pending (the run page owns a live run), so it is drawn so. */}
            <ol className="mt-1.5 max-sm:hidden">
              <li className="flex items-center gap-3.5 px-4 py-3.5 text-sm text-slate-600">
                <span aria-hidden="true" className={`${BADGE_CLASS} border border-slate-300`}>
                  3
                </span>
                <span className="flex flex-col">
                  <span className="font-semibold">{STEP_RAIL_TAILOR_LABEL}</span>
                  <span className="text-xs text-slate-500">{STEP_RAIL_TAILOR_CAPTION}</span>
                </span>
              </li>
            </ol>
          </>
        )}
      </div>

      {/* The panel column is always the second child, so the panel wrappers keep their fibers —
          and the panels their state and query observers — when the rail fills in. */}
      <div className="min-w-0">
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
        {children}
      </div>
    </div>
  );
}
