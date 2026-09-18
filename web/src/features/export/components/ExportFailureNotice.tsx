/**
 * What a control says when its job ended in `failed`.
 *
 * It renders **into** the control's `role="status"` region rather than wrapping itself in one: it
 * returns a fragment, so the sentence becomes a text node of that live region and the retry button
 * its sibling. That is why there is no container element here. A second live region nested inside
 * the control's own would announce the same words twice to a screen reader, and a `role="alert"`
 * would interrupt whatever the user was doing for a failure that is four seconds old and sits next
 * to three other controls.
 */

import { EXPORT_AGAIN_ACTION, exportFailureCopyFor } from '../exportCopy';

import type { ExportFailureReason, ExportFormat } from '../types';

export interface ExportFailureNoticeProps {
  /**
   * The job's `failure_reason`, **exactly as the API sent it** — and nullable, because that is what
   * the wire carries: one `ExportJobResponse` schema serves all four statuses, so `failure_reason`
   * is `ExportFailureReason | null` on a `failed` job too. The row's `CHECK` does guarantee it, and
   * asserting a database constraint from TypeScript with a `!` is the thing this project does not
   * do (Constitution §4.5). 1.3 settled it one context over: `TailoringFailureNoticeProps.reason`
   * is `TailoringFailureReason | null` and renders a generic headline for `null`.
   */
  readonly reason: ExportFailureReason | null;
  /**
   * Which format failed — the sentence names it (*"…into a PDF"*), so the user reading four
   * controls at once knows which one is talking.
   */
  readonly format: ExportFormat;
  /**
   * Whether *Export again* is worth offering — **the API's answer, read, never re-derived**
   * (AC-24). Which failures are worth paying a second worker render for is a business rule;
   * `render_failed`, `output_too_large` and `source_unavailable` are `false` because the same input
   * would fail the same way, and a TypeScript copy of that list would be a second authority that
   * drifts.
   */
  readonly retryable: boolean;
  /** Re-request this (document, format). Offered only when `retryable`. */
  readonly onRetry: () => void;
}

export function ExportFailureNotice({
  reason,
  format,
  retryable,
  onRetry,
}: ExportFailureNoticeProps): React.JSX.Element {
  return (
    <>
      {exportFailureCopyFor(reason, format)}
      {retryable && (
        <button
          type="button"
          onClick={onRetry}
          className="ml-2 rounded-md px-1.5 py-0.5 font-medium text-amber-800 underline underline-offset-2 hover:bg-amber-100"
        >
          {EXPORT_AGAIN_ACTION}
        </button>
      )}
    </>
  );
}
