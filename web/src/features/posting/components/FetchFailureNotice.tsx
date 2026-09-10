/**
 * **This is FR-2's flash message, and the button is the whole point of the component.**
 *
 * The requirement says: "If scraping fails … the UI shall display a flash error message prompting
 * manual copy-paste input." A sentence saying "try pasting it instead" satisfies that literally and
 * leaves the user exactly where they were — looking at a form, holding a link that does not work,
 * with the next step being their problem. The difference between satisfying FR-2 and actually
 * getting someone unstuck is a control that *performs* the fallback.
 *
 * So this offers three things, in order of how much they help:
 *
 * 1. **A button that switches the input to paste mode and focuses the textarea.** One click, and
 *    the user is typing rather than deciding what to do.
 * 2. **The URL, still visible, as a link.** They need to open the posting in a tab to copy from it,
 *    and re-typing a URL they already gave us would be an insult.
 * 3. **Wording that does not blame them.** The link was fine; the site refused us. "We couldn't
 *    read" rather than "invalid URL".
 *
 * Styled amber rather than red, deliberately, and the same call `UploadErrorNotice` makes for its
 * error C: nothing is broken and nothing was lost — there is simply a different route to the same
 * outcome. Red would say "something went wrong", which invites a retry of the thing that will fail
 * identically.
 */
export interface FetchFailureNoticeProps {
  readonly message: string;
  readonly url: string;
  readonly onPasteInstead: () => void;
}

export function FetchFailureNotice({
  message,
  url,
  onPasteInstead,
}: FetchFailureNoticeProps): React.JSX.Element {
  return (
    <div
      role="alert"
      className="space-y-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2"
    >
      <p className="text-sm font-medium text-amber-900">{message}</p>

      {url !== '' && (
        <p className="text-sm break-all text-amber-800">
          <a href={url} target="_blank" rel="noopener noreferrer nofollow" className="underline">
            {url}
          </a>
        </p>
      )}

      <button
        type="button"
        onClick={onPasteInstead}
        className="rounded-md bg-amber-800 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-900"
      >
        Paste the description instead
      </button>
    </div>
  );
}
