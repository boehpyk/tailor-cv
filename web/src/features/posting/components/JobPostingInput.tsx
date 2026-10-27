import type { PostingSource } from '../types';

/**
 * The two ways to give the system a job description. Presentational: it formats what it is given
 * and owns no server state.
 *
 * **A real `<fieldset>` with two radios, not a div-based segmented control.** The segmented control
 * is the fashionable shape and it is unreachable by keyboard unless you re-implement arrow-key
 * navigation, focus management and `aria-checked` by hand — which is re-implementing a radio group
 * badly. A native radio group gets all of that, plus screen-reader announcement of "2 of 2", for
 * free. So the segmented *look* (web-design-refresh AC-16) is CSS over the same radios: each input
 * is `sr-only` — still focusable, still the browser's arrow-key group — and its `<label>` draws the
 * segment, reading the input's state through `has-checked:` and `has-focus-visible:`.
 *
 * Paste is the default mode because **paste always works**. The link is the optimistic path: it
 * saves typing when the site lets us read it, and roughly half of the job boards people actually
 * use will refuse (FR-2 exists for exactly that). Defaulting to the path that cannot fail means the
 * first thing a user sees is the thing that will work.
 */
export interface JobPostingInputProps {
  readonly mode: PostingSource;
  readonly text: string;
  readonly url: string;
  readonly disabled: boolean;
  readonly maxCharacters: number;
  readonly textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  readonly onModeChange: (mode: PostingSource) => void;
  readonly onTextChange: (text: string) => void;
  readonly onUrlChange: (url: string) => void;
}

/** One segment of the toggle: the label is the visible control, the radio inside it the real one. */
const SEGMENT_CLASS =
  'flex min-h-11 cursor-pointer items-center justify-center rounded-full px-4 text-sm font-semibold text-slate-600 hover:text-slate-900 has-checked:bg-white has-checked:text-slate-900 has-checked:shadow-sm has-focus-visible:outline-2 has-focus-visible:outline-offset-2 has-focus-visible:outline-brand-600 sm:min-h-10';

const FIELD_CLASS =
  'w-full rounded-xl border border-slate-300 bg-white p-3 text-sm text-slate-900 focus-visible:border-brand-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600';

export function JobPostingInput({
  mode,
  text,
  url,
  disabled,
  maxCharacters,
  textareaRef,
  onModeChange,
  onTextChange,
  onUrlChange,
}: JobPostingInputProps): React.JSX.Element {
  // The counter measures what the user can see, which is the same number the API's
  // `character_count` reports and the same one its 30,000 ceiling is measured in. That agreement is
  // deliberate: measuring the limit in one unit and displaying another is how a UI ends up showing
  // "35,999 / 30,000" for text the server just accepted.
  const characterCount = text.length;
  const overLimit = characterCount > maxCharacters;

  return (
    <fieldset className="space-y-4 border-0 p-0" disabled={disabled}>
      <legend className="sr-only">How would you like to add the job posting?</legend>

      <div className="inline-grid grid-cols-2 gap-1 rounded-full bg-surface-sunk p-1 max-sm:grid max-sm:w-full">
        <label className={SEGMENT_CLASS}>
          <input
            type="radio"
            name="posting-source"
            value="pasted"
            checked={mode === 'pasted'}
            onChange={() => {
              onModeChange('pasted');
            }}
            className="sr-only"
          />
          Paste the text
        </label>
        <label className={SEGMENT_CLASS}>
          <input
            type="radio"
            name="posting-source"
            value="fetched"
            checked={mode === 'fetched'}
            onChange={() => {
              onModeChange('fetched');
            }}
            className="sr-only"
          />
          Link to the posting
        </label>
      </div>

      {mode === 'pasted' ? (
        <div className="space-y-1">
          <label htmlFor="posting-text" className="block text-sm font-medium text-slate-900">
            Job posting text
          </label>
          <textarea
            id="posting-text"
            ref={textareaRef}
            rows={8}
            value={text}
            onChange={(event) => {
              onTextChange(event.target.value);
            }}
            className={FIELD_CLASS}
            placeholder="Paste the job description here…"
          />
          <p
            data-over-limit={overLimit ? 'true' : undefined}
            className={`text-xs ${overLimit ? 'font-medium text-danger-600' : 'text-slate-500'}`}
          >
            {characterCount.toLocaleString('en-GB')} / {maxCharacters.toLocaleString('en-GB')}
          </p>
        </div>
      ) : (
        <div className="space-y-1">
          <label htmlFor="posting-url" className="block text-sm font-medium text-slate-900">
            Job posting URL
          </label>
          <input
            id="posting-url"
            type="url"
            value={url}
            onChange={(event) => {
              onUrlChange(event.target.value);
            }}
            className={FIELD_CLASS}
            placeholder="https://…"
          />
        </div>
      )}
    </fieldset>
  );
}
