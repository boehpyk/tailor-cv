import { formatStoredUntil } from '@/features/intake/format';

import type { JobPostingSummary } from '../types';

/**
 * A captured job posting. Presentational.
 *
 * The host is shown as a link rather than the full URL, for the same reason the API only ever logs
 * a host: a job-posting URL names the specific job a specific person is applying for, and its query
 * string routinely carries tracking parameters and sometimes a referral token. The host is enough
 * for a person to recognise where a posting came from.
 */
export interface JobPostingCardProps {
  readonly posting: JobPostingSummary;
  readonly onReplace: () => void;
}

/** The host of a URL, or `null` if it cannot be parsed. Never throws: a stored URL has already been
 * through `SourceUrl`'s rules server-side, but a card should not blank the page if that ever
 * changes. */
function hostOf(url: string): string | null {
  try {
    return new URL(url).host;
  } catch {
    return null;
  }
}

export function JobPostingCard({ posting, onReplace }: JobPostingCardProps): React.JSX.Element {
  const host = posting.source_url !== null ? hostOf(posting.source_url) : null;

  return (
    <div className="space-y-2 rounded-md border border-slate-200 bg-white px-4 py-3">
      <div className="flex items-start justify-between gap-4">
        <div>
          {/* "Job posting" rather than an invented label when the page had no title (J-4: a pasted
              posting never has one). A guessed title — the first line of a clipboard paste, say —
              would be a confidently wrong label, which is worse than an honest generic one. */}
          <h3 className="font-medium text-slate-900">{posting.title ?? 'Job posting'}</h3>
          <p className="text-sm text-slate-500">
            {/* Unformatted, matching `BaseCvCard`'s "N characters extracted". The INPUT's live
                counter is formatted (`3,184 / 30,000`) because it is a number being read against a
                limit; a count on a card is a fact, and the two do not have to agree on style. */}
            {posting.character_count} characters
            {host !== null && (
              <>
                {' · '}
                <a
                  href={posting.source_url ?? undefined}
                  target="_blank"
                  // `noopener` and `noreferrer` because this is a link to a host a stranger chose:
                  // without them the target page gets a `window.opener` handle back into this tab.
                  // `nofollow` because we are not vouching for it.
                  rel="noopener noreferrer nofollow"
                  className="underline hover:text-slate-700"
                >
                  {host}
                </a>
              </>
            )}
          </p>
        </div>
        <button
          type="button"
          onClick={onReplace}
          className="shrink-0 text-sm text-slate-600 underline hover:text-slate-900"
        >
          Replace posting
        </button>
      </div>

      <p className="text-sm text-slate-600">{posting.preview}</p>

      <p className="text-xs text-slate-500">Stored until {formatStoredUntil(posting.expires_at)}</p>
    </div>
  );
}
