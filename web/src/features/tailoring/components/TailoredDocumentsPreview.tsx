import { useId } from 'react';

/** One tailored document as the preview needs it: the text, and the server's count of it. */
export interface PreviewDocument {
  /** Untrusted model output. Rendered as a text node, never as markup (AC-31). */
  readonly text: string;
  /**
   * The API's `*_character_count`, shown as-is rather than recomputed with `text.length`: the
   * server counts code points and JavaScript counts UTF-16 units, so the two disagree on any CV with
   * an emoji or a rare script — and the server's number is the one its limits are measured in.
   */
  readonly characterCount: number | null;
}

export interface TailoredDocumentsPreviewProps {
  readonly tailoredCv: PreviewDocument;
  readonly coverLetter: PreviewDocument;
}

/** Pinned rather than the browser's locale, for the reason `formatStoredUntil` gives. */
const COUNT_FORMAT = new Intl.NumberFormat('en-US');

function DocumentPane({
  title,
  doc,
}: {
  readonly title: string;
  readonly doc: PreviewDocument;
}): React.JSX.Element {
  const headingId = useId();

  return (
    <section aria-labelledby={headingId} className="rounded-md border border-slate-200 bg-white">
      <header className="flex items-baseline justify-between gap-3 border-b border-slate-200 px-3 py-2">
        <h3 id={headingId} className="text-sm font-medium text-slate-900">
          {title}
        </h3>
        {doc.characterCount !== null && (
          <p className="text-xs text-slate-500 tabular-nums">
            {`${COUNT_FORMAT.format(doc.characterCount)} characters`}
          </p>
        )}
      </header>
      {/* The model's output goes in as a React child — a text node. React escapes it, so a
          `<script>` in a tailored CV is shown as the characters `<script>`, which is also the only
          honest thing to show: it is what the model wrote. `whitespace-pre-wrap` keeps the
          document's own line breaks without a markdown renderer, which would be a second parser
          of hostile input (G-33). Focusable because it scrolls: a keyboard user must be able to
          reach the bottom of a long CV. */}
      <div
        tabIndex={0}
        className="max-h-96 overflow-y-auto px-3 py-2 text-sm whitespace-pre-wrap text-slate-800"
      >
        {doc.text}
      </div>
    </section>
  );
}

/**
 * The success state — presentational. Two labelled, read-only panes and their character counts.
 *
 * **No `dangerouslySetInnerHTML`, and no markdown renderer** (AC-31, G-33). The documents are
 * untrusted text written by a model that read a job posting a stranger chose, which makes them the
 * most attacker-influenced strings in the product. Sanitizing them into HTML is slice 1.4's job,
 * when the editor genuinely needs markup; until then the safest renderer is the one React already
 * has.
 */
export function TailoredDocumentsPreview({
  tailoredCv,
  coverLetter,
}: TailoredDocumentsPreviewProps): React.JSX.Element {
  return (
    <div className="space-y-4">
      <DocumentPane title="Tailored CV" doc={tailoredCv} />
      <DocumentPane title="Cover letter" doc={coverLetter} />
      <p className="text-sm text-slate-500">
        This is a read-only preview. Editing and downloads come next.
      </p>
    </div>
  );
}
