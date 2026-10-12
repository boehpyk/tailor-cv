export interface BodyTextProps {
  /** Untrusted text (a CV, a posting, an LLM document). Shown as a text node, never as markup. */
  readonly text: string;
}

/**
 * **The one place an account's body is shown** (AC-25): a `white-space: pre-wrap` block holding a
 * text node — never `dangerouslySetInnerHTML`, never a Markdown renderer.
 *
 * SKELETON (T11): a distinguishable placeholder.
 */
export function BodyText({ text }: BodyTextProps): React.JSX.Element {
  return <div className="whitespace-pre-wrap break-words" data-length={text.length} />;
}
