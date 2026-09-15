import { Link } from 'react-router';

import type { SaveState } from '../saveState';
import type { TailoredDocumentKind } from '@/features/tailoring/types';

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

/**
 * The two document tabs — presentational. A `role="tablist"` of two `<Link>`s to
 * `/runs/:id/cv` and `/runs/:id/cover_letter`: **the URL is the selected tab**, so switching is a
 * navigation and the back button undoes it. Unlike `InputTabs`, whose selection is a search
 * parameter, these are path segments — a document is a place you can link someone to.
 *
 * A tab whose document is anything but `saved` carries an *unsaved* marker, so a person editing
 * the letter can see the CV still has something pending (AC-30). It is a marker, not the state's
 * sentence — the sentence belongs to the visible document's `SaveIndicator`.
 */
export function DocumentTabs({ runId, selected, states }: DocumentTabsProps): React.JSX.Element {
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
            role="tab"
            aria-selected={isSelected}
            aria-controls={`document-${kind}`}
            tabIndex={isSelected ? 0 : -1}
            to={`/runs/${encodeURIComponent(runId)}/${kind}`}
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
