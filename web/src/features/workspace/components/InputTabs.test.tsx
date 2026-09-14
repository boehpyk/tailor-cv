import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';

import { InputTabs } from './InputTabs';

import type { WorkspaceTab } from '../hooks/useWorkspaceTab';

/**
 * F6 RED — `InputTabs`'s behavioural contract (feature-spec AC-23; technical-plan "Frontend"
 * §`InputTabs.tsx`: "presentational... **both mounted**, the inactive one `hidden`... Arrow-key
 * navigation between tabs").
 *
 * Written against the **spec**, not `InputTabs.tsx`'s F5 skeleton, which renders the roles and the
 * `id`/`aria-controls`/`aria-labelledby` wiring but reads neither `tab` nor `onTabChange` (they are
 * not even destructured) and gives the tab buttons no `onClick` or `onKeyDown` — so `aria-selected`,
 * the inactive panel's `hidden` attribute, and every keyboard-navigation assertion below fail on a
 * genuine mismatch (a missing attribute, a focus that never moved), not on an `ImportError`.
 *
 * `InputTabs` is controlled (`tab` + `onTabChange`), so this file wraps it in a small stateful
 * harness that plays the role `WorkspacePage` will in F7 — reflecting `onTabChange` back into
 * `tab` — so the tests exercise the real controlled contract rather than a fixed prop.
 *
 * The panel content is synthetic (`<input>` fixtures), not the real `BaseCvUploadPanel` /
 * `JobPostingPanel` — `InputTabs` is documented as presentational and takes `ReactNode`, so its own
 * contract does not depend on what the panels are; using the real panels here would drag in two
 * more query clients and `fetch` stubs for a component that never reads a query of its own. The
 * "half-typed posting survives a tab switch" half of AC-23, which specifically needs the real paste
 * textarea, is `WorkspacePage.test.tsx`'s job instead.
 */

/**
 * Locates a tabpanel through the ARIA wiring (`aria-controls` on the corresponding tab) rather than
 * through RTL's accessible-name role query. `getByRole('tabpanel', { name, hidden: true })` can
 * never match a hidden panel: `dom-accessibility-api`'s accname algorithm (step 2A) returns `""` for
 * any root carrying the `hidden` attribute, before it ever looks at `aria-labelledby` — proven on a
 * bare `<div role="tabpanel" aria-labelledby hidden>`, where `queryByRole` with `{ name, hidden:
 * true }` is `null` regardless of what the label says. The tab -> `aria-controls` -> panel `id` path
 * is unaffected by the panel's own hidden state.
 */
function panelControlledBy(tabName: string): HTMLElement {
  const tab = screen.getByRole('tab', { name: tabName });
  const panelId = tab.getAttribute('aria-controls');
  if (panelId === null) {
    throw new Error(`tab "${tabName}" has no aria-controls`);
  }
  const panel = document.getElementById(panelId);
  if (panel === null) {
    throw new Error(`no element with id "${panelId}" for tab "${tabName}"`);
  }
  return panel;
}

function StatefulInputTabs({
  initial = 'base-cv',
}: { initial?: WorkspaceTab } = {}): React.JSX.Element {
  const [tab, setTab] = useState<WorkspaceTab>(initial);
  return (
    <InputTabs
      tab={tab}
      onTabChange={setTab}
      baseCvPanel={<input aria-label="base cv fixture" defaultValue="" />}
      jobPostingPanel={<input aria-label="job posting fixture" defaultValue="" />}
    />
  );
}

describe('InputTabs', () => {
  it('renders a tablist of two tabs and two tabpanels', () => {
    render(<StatefulInputTabs />);

    expect(screen.getByRole('tablist')).toBeInTheDocument();
    expect(screen.getAllByRole('tab')).toHaveLength(2);
    // Both panels are mounted regardless of which is active (AC-23) — `hidden: true` reaches into
    // the accessibility tree RTL's role query otherwise excludes an inactive panel from.
    expect(screen.getAllByRole('tabpanel', { hidden: true })).toHaveLength(2);
  });

  it('marks the Base CV tab selected and the Job posting panel hidden by default', () => {
    render(<StatefulInputTabs initial="base-cv" />);

    expect(screen.getByRole('tab', { name: 'Base CV' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: 'Job posting' })).toHaveAttribute(
      'aria-selected',
      'false',
    );

    const basePanel = screen.getByRole('tabpanel', { name: 'Base CV' });
    // `getByRole('tabpanel', { name, hidden: true })` can never find this panel — see
    // `panelControlledBy`'s docstring — so locate it via the tab's `aria-controls` instead.
    const postingPanel = panelControlledBy('Job posting');
    expect(basePanel).toBeInTheDocument();
    expect(postingPanel).toBeInTheDocument();
    expect(postingPanel).toHaveAttribute('hidden');
    expect(basePanel).not.toHaveAttribute('hidden');
  });

  it('marks the Job posting tab selected and the Base CV panel hidden when that tab is active', () => {
    render(<StatefulInputTabs initial="job-posting" />);

    expect(screen.getByRole('tab', { name: 'Job posting' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.getByRole('tab', { name: 'Base CV' })).toHaveAttribute('aria-selected', 'false');
    // Same reasoning as above — the hidden panel is unreachable by name, so go through
    // `aria-controls` (see `panelControlledBy`'s docstring).
    expect(panelControlledBy('Base CV')).toHaveAttribute('hidden');
    expect(screen.getByRole('tabpanel', { name: 'Job posting' })).not.toHaveAttribute('hidden');
  });

  it('clicking the inactive tab selects it', async () => {
    const user = userEvent.setup();
    render(<StatefulInputTabs initial="base-cv" />);

    await user.click(screen.getByRole('tab', { name: 'Job posting' }));

    expect(screen.getByRole('tab', { name: 'Job posting' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
  });

  it('ArrowRight moves both the selection and focus from Base CV to Job posting', async () => {
    const user = userEvent.setup();
    render(<StatefulInputTabs initial="base-cv" />);

    screen.getByRole('tab', { name: 'Base CV' }).focus();
    await user.keyboard('{ArrowRight}');

    const jobPostingTab = screen.getByRole('tab', { name: 'Job posting' });
    expect(jobPostingTab).toHaveAttribute('aria-selected', 'true');
    expect(jobPostingTab).toHaveFocus();
  });

  it('ArrowLeft moves both the selection and focus from Job posting to Base CV', async () => {
    const user = userEvent.setup();
    render(<StatefulInputTabs initial="job-posting" />);

    screen.getByRole('tab', { name: 'Job posting' }).focus();
    await user.keyboard('{ArrowLeft}');

    const baseCvTab = screen.getByRole('tab', { name: 'Base CV' });
    expect(baseCvTab).toHaveAttribute('aria-selected', 'true');
    expect(baseCvTab).toHaveFocus();
  });

  it('ArrowRight wraps from the last tab back to the first', async () => {
    const user = userEvent.setup();
    render(<StatefulInputTabs initial="job-posting" />);

    screen.getByRole('tab', { name: 'Job posting' }).focus();
    await user.keyboard('{ArrowRight}');

    const baseCvTab = screen.getByRole('tab', { name: 'Base CV' });
    expect(baseCvTab).toHaveAttribute('aria-selected', 'true');
    expect(baseCvTab).toHaveFocus();
  });

  it('Home selects and focuses the first tab from anywhere', async () => {
    const user = userEvent.setup();
    render(<StatefulInputTabs initial="job-posting" />);

    screen.getByRole('tab', { name: 'Job posting' }).focus();
    await user.keyboard('{Home}');

    const baseCvTab = screen.getByRole('tab', { name: 'Base CV' });
    expect(baseCvTab).toHaveAttribute('aria-selected', 'true');
    expect(baseCvTab).toHaveFocus();
  });

  it('End selects and focuses the last tab from anywhere', async () => {
    const user = userEvent.setup();
    render(<StatefulInputTabs initial="base-cv" />);

    screen.getByRole('tab', { name: 'Base CV' }).focus();
    await user.keyboard('{End}');

    const jobPostingTab = screen.getByRole('tab', { name: 'Job posting' });
    expect(jobPostingTab).toHaveAttribute('aria-selected', 'true');
    expect(jobPostingTab).toHaveFocus();
  });
});
