/**
 * What a control says when its job ended in `failed` — **SKELETON (F4). It renders nothing.**
 *
 * The props are the contract; the body is empty. No sentence, no *Export again*, not even the
 * container element — because **where** a failure is announced (its own `role="alert"`, or the
 * words placed inside the queued control's existing `role="status"` region) is a decision that
 * belongs with the copy it announces, and both are F6.
 *
 * The bar does not render this component yet, and cannot: nothing in F4 derives a view, so nothing
 * knows a job failed. That is the SKELETON's whole point — F5's failure rows must red on the
 * sentence they assert, not on a missing import. Four slices (**1.1's T33/T34, 1.3's T40, 1.4's F5
 * and F8**) shipped a working frontend skeleton and made `qa`'s tests pass on arrival; this one does
 * not.
 */

import type { ExportFailureReason } from '../types';

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

export function ExportFailureNotice(props: ExportFailureNoticeProps): React.JSX.Element {
  // Declared and deliberately unread; `noUnusedParameters` is on and `_props` would hide the
  // signature. F6 deletes this line by using all three fields. The rule below is right in general
  // and wrong for the one case it exists to catch, so it is disabled by name, for one line.
  // eslint-disable-next-line @typescript-eslint/no-meaningless-void-operator
  void props;

  return <></>;
}
