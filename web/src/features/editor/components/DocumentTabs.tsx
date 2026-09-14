import { Link } from 'react-router';

import type { SaveState } from '../saveState';
import type { TailoredDocumentKind } from '@/features/tailoring/types';

export interface DocumentTabsProps {
  readonly runId: string;
  /** Each document's save state — the source of the "unsaved" marker on a tab (AC-30). */
  readonly states: Readonly<Record<TailoredDocumentKind, SaveState>>;
}

/** Display order, and the URL segment each tab is. */
const TAB_ORDER: readonly TailoredDocumentKind[] = ['cv', 'cover_letter'];

/**
 * The two document tabs — presentational. A `role="tablist"` of two `<Link>`s to
 * `/runs/:id/cv` and `/runs/:id/cover_letter`: **the URL is the selected tab**, so switching is a
 * navigation and the back button undoes it. Unlike `InputTabs`, whose selection is a search
 * parameter, these are path segments — a document is a place you can link someone to.
 *
 * Skeleton (F8): the links and the roles. No selected state, no unsaved marker; F10b adds both and
 * is the first to read `states`, which is why it is typed here and not destructured yet.
 */
export function DocumentTabs({ runId }: DocumentTabsProps): React.JSX.Element {
  return (
    <div role="tablist" className="mb-4 flex gap-1 border-b border-slate-200">
      {TAB_ORDER.map((kind) => (
        <Link
          key={kind}
          role="tab"
          to={`/runs/${encodeURIComponent(runId)}/${kind}`}
          className="-mb-px border-b-2 border-transparent px-3 py-2 text-sm font-medium text-slate-500"
        >
          {kind}
        </Link>
      ))}
    </div>
  );
}
