export interface BodyTextProps {
  /** Untrusted text (a CV, a posting, an LLM document). Shown as a text node, never as markup. */
  readonly text: string;
}

/**
 * **The one place an account's body is shown** (AC-25): a `white-space: pre-wrap` block holding a
 * text node. React escapes it, so `<img onerror>` and `**bold**` read as those characters — never
 * `dangerouslySetInnerHTML`, never a Markdown renderer. `break-words` keeps a 250 000-character
 * line inside 360 px. Labelled by the heading above it, in its section.
 */
export function BodyText({ text }: BodyTextProps): React.JSX.Element {
  return (
    <div className="rounded-md border border-slate-200 bg-white p-4 text-sm whitespace-pre-wrap break-words text-slate-800">
      {text}
    </div>
  );
}
