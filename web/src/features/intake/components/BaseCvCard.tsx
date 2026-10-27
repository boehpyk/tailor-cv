import { WORKING_COPY_BADGE, WORKING_COPY_NOTE } from '@/features/savedCvs/savedCvsCopy';

import { formatSize, formatStoredUntil } from '../format';

import type { BaseCv } from '../types';

interface BaseCvCardProps {
  /** An extracted `BaseCv` (`status === 'extracted'`) — the caller decides that, not this component. */
  readonly cv: BaseCv;
  readonly onReplace: () => void;
}

/**
 * The success view — presentational: filename, size, character count, "stored until …",
 * "Replace CV" (technical-plan.md's Components list). Each fact keeps its own text node rather
 * than being joined into one sentence, because the component test queries them by exact text
 * individually (`screen.getByText('2 KB')`, `screen.getByText('512 characters extracted')`).
 */
export function BaseCvCard({ cv, onReplace }: BaseCvCardProps): React.JSX.Element {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 sm:p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 space-y-1">
          <p className="font-semibold break-words text-slate-900">{cv.original_filename}</p>
          {cv.origin === 'copied_from_saved' && (
            // AC-41: a working copy is guest data like any upload, and says so — the saved CV it
            // came from stays in the account; this copy goes with the workspace.
            <>
              <p>
                <span className="inline-block rounded-full bg-brand-50 px-2.5 py-0.5 text-xs font-medium text-brand-700">
                  {WORKING_COPY_BADGE}
                </span>
              </p>
              <p className="text-sm text-slate-600">{WORKING_COPY_NOTE}</p>
            </>
          )}
          <p className="text-sm text-slate-500">{formatSize(cv.size_bytes)}</p>
          <p className="text-sm text-slate-500">{cv.character_count ?? 0} characters extracted</p>
          <p className="text-sm text-slate-500">stored until {formatStoredUntil(cv.expires_at)}</p>
        </div>
        <button
          type="button"
          onClick={onReplace}
          className="min-h-11 shrink-0 rounded-lg border border-slate-300 bg-white px-3 text-sm font-semibold text-slate-700 hover:bg-surface-sunk hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 sm:min-h-9"
        >
          Replace CV
        </button>
      </div>
    </div>
  );
}
