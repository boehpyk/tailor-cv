import { useId } from 'react';

import type { WorkspaceTab } from '../hooks/useWorkspaceTab';
import type { ReactNode } from 'react';

export interface InputTabsProps {
  readonly tab: WorkspaceTab;
  readonly onTabChange: (tab: WorkspaceTab) => void;
  /** The base-CV panel — mounted whichever tab is open, so an upload in flight is never unmounted. */
  readonly baseCvPanel: ReactNode;
  /** The job-posting panel — mounted whichever tab is open, so a half-typed posting survives. */
  readonly jobPostingPanel: ReactNode;
}

/**
 * The two input tabs — presentational (AC-23). `role="tablist"`, two `role="tab"`s, two
 * `role="tabpanel"`s, **both mounted**.
 *
 * F5 skeleton: the roles and their wiring (`id` ↔ `aria-controls` ↔ `aria-labelledby`) and nothing
 * else. Selection, `hidden` on the inactive panel and arrow-key navigation arrive in F7 against qa's
 * red tests.
 */
export function InputTabs({ baseCvPanel, jobPostingPanel }: InputTabsProps): React.JSX.Element {
  const baseCvTabId = useId();
  const baseCvPanelId = useId();
  const jobPostingTabId = useId();
  const jobPostingPanelId = useId();

  return (
    <div>
      <div role="tablist" aria-label="Your inputs">
        <button type="button" role="tab" id={baseCvTabId} aria-controls={baseCvPanelId}>
          Base CV
        </button>
        <button type="button" role="tab" id={jobPostingTabId} aria-controls={jobPostingPanelId}>
          Job posting
        </button>
      </div>

      <div role="tabpanel" id={baseCvPanelId} aria-labelledby={baseCvTabId}>
        {baseCvPanel}
      </div>
      <div role="tabpanel" id={jobPostingPanelId} aria-labelledby={jobPostingTabId}>
        {jobPostingPanel}
      </div>
    </div>
  );
}
