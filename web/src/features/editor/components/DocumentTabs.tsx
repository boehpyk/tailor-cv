import { useRef } from 'react';
import { Link, useNavigate } from 'react-router';

import { runLink } from '@/features/scope/scopeMap';
import { useScopeMap } from '@/features/scope/useWorkspaceScope';

import type { SaveState } from '../saveState';
import type { TailoredDocumentKind } from '@/features/tailoring/types';
import type { KeyboardEvent } from 'react';

export interface DocumentTabsProps {
  readonly runId: string;
  /** The document the URL names — the selected tab. */
  readonly selected: TailoredDocumentKind;
  /** Each document's save state — the source of the "unsaved" marker on a tab (AC-30). */
  readonly states: Readonly<Record<TailoredDocumentKind, SaveState>>;
}

/** Display order, and the URL segment each tab is. */
const TAB_ORDER: readonly TailoredDocumentKind[] = ['cv', 'cover_letter'];

const TAB_LABELS: Readonly<Record<TailoredDocumentKind, string>> = {
  cv: 'CV',
  cover_letter: 'Cover letter',
};

/** Where ←/→ (wrapping), Home or End moves the selection from `current`; `null` for any other key. */
function tabAfterKey(current: TailoredDocumentKind, key: string): TailoredDocumentKind | null {
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
 * The two document tabs — presentational. A `role="tablist"` of two `<Link>`s to the run's two
 * document URLs — `/runs/:id/…` for a guest, `/history/:id/…` for an account (the route's scope,
 * slice 2.3; a history run's tab must never jump to the guest route) — **the URL is the selected tab**, so switching is a
 * navigation and the back button undoes it. Unlike `InputTabs`, whose selection is a search
 * parameter, these are path segments — a document is a place you can link someone to.
 *
 * A tab whose document is anything but `saved` carries an *unsaved* marker, so a person editing
 * the letter can see the CV still has something pending (AC-30). It is a marker, not the state's
 * sentence — the sentence belongs to the visible document's `SaveIndicator`.
 *
 * **Keyboard** (web-design-refresh AC-19): the ARIA tabs pattern with automatic activation, as in
 * `InputTabs` — ←/→ wrap, Home/End jump, and each is the same navigation a click makes, to the
 * scope's `runLink`, so `DocumentWorkspace`'s blocker exempts it exactly as it exempts the click
 * (it judges the destination URL, not the gesture). Focus moves imperatively from the handler: both
 * links stay mounted across the switch (one route element serves both documents), and an effect
 * watching `selected` would also grab focus on a back-button navigation.
 */
export function DocumentTabs({ runId, selected, states }: DocumentTabsProps): React.JSX.Element {
  const map = useScopeMap();
  const navigate = useNavigate();
  const tabRefs = useRef<Record<TailoredDocumentKind, HTMLAnchorElement | null>>({
    cv: null,
    cover_letter: null,
  });

  function handleKeyDown(
    event: KeyboardEvent<HTMLAnchorElement>,
    from: TailoredDocumentKind,
  ): void {
    const next = tabAfterKey(from, event.key);
    if (next === null) {
      return;
    }
    event.preventDefault();
    if (next !== selected) {
      void navigate(runLink(map, runId, next));
    }
    tabRefs.current[next]?.focus();
  }

  return (
    <div
      role="tablist"
      aria-label="Your documents"
      className="mb-4 flex gap-1 border-b border-slate-200"
    >
      {TAB_ORDER.map((kind) => {
        const isSelected = kind === selected;
        const unsaved = states[kind].kind !== 'saved';
        return (
          <Link
            key={kind}
            ref={(element) => {
              tabRefs.current[kind] = element;
            }}
            role="tab"
            aria-selected={isSelected}
            aria-controls={`document-${kind}`}
            tabIndex={isSelected ? 0 : -1}
            to={runLink(map, runId, kind)}
            onKeyDown={(event) => {
              handleKeyDown(event, kind);
            }}
            className={
              isSelected
                ? '-mb-px border-b-2 border-slate-900 px-3 py-2 text-sm font-medium text-slate-900'
                : '-mb-px border-b-2 border-transparent px-3 py-2 text-sm font-medium text-slate-500 hover:text-slate-800'
            }
          >
            {TAB_LABELS[kind]}
            {unsaved && (
              <span className="ml-1.5 text-xs font-normal text-amber-700" title="Unsaved changes">
                (unsaved)
              </span>
            )}
          </Link>
        );
      })}
    </div>
  );
}
