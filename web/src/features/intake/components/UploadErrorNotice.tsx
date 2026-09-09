/**
 * The three upload-error states, modelled as a discriminated union rather than a shared shape with
 * optional fields (CLAUDE.md) — they are mutually exclusive facts (AC-15) and a reader should not
 * be able to construct "apiError with no status" by omission.
 *
 * `preValidation` and `extractionFailed` carry only the message the container already computed
 * (from `preValidate` and `failure_message` respectively); `apiError` carries the primitives this
 * component maps into an action hint, so it stays decoupled from the `ApiError` class itself.
 */
export type UploadErrorNoticeProps =
  | { readonly kind: 'preValidation'; readonly message: string }
  | {
      readonly kind: 'apiError';
      readonly message: string;
      readonly status: number;
      readonly code: string | null;
    }
  | { readonly kind: 'extractionFailed'; readonly message: string };

/**
 * A short, actionable follow-up for a known API error code — the technical-plan's "the server's
 * message, with the action" (e.g. "Try a PDF, DOCX or TXT file"). Branches on `code`, the stable
 * contract (client.ts's docstring), never on `message`; `status` is only the fallback for a
 * transport-level failure that has no code at all (a network error, or a 5xx with no envelope).
 */
function actionHintFor(status: number, code: string | null): string | null {
  switch (code) {
    case 'unsupported_format':
      return 'Try a PDF, DOCX or TXT file.';
    case 'file_too_large':
      return 'Try a smaller file, up to 10 MB.';
    case 'too_many_base_cvs':
      return 'Remove an older CV before uploading a new one.';
    case 'rate_limited':
      return 'Wait a few minutes, then try again.';
    default:
      return status >= 500 ? 'This looks like a temporary problem — try again shortly.' : null;
  }
}

/**
 * Maps an upload failure to an actionable sentence — presentational, no state, no fetching.
 *
 * The three kinds are **visibly and textually distinct** (AC-15): error C ("your file is
 * unreadable") is not styled as a network failure, because only one of the three is fixed by
 * retrying the same file, and a user who cannot tell which will refresh and pay for nothing.
 */
export function UploadErrorNotice(props: UploadErrorNoticeProps): React.JSX.Element {
  if (props.kind === 'preValidation') {
    // Error A — rejected before any request was made. Inline text, not a boxed notice: nothing
    // was sent, so this reads closer to a form validation message than an incident.
    return (
      <p role="alert" className="text-sm font-medium text-red-700">
        {props.message}
      </p>
    );
  }

  if (props.kind === 'apiError') {
    // Error B — the API rejected the request (413 / 415 / 422 / 409 / 429 / 5xx).
    const hint = actionHintFor(props.status, props.code);
    return (
      <div role="alert" className="space-y-1 rounded-md border border-red-200 bg-red-50 px-3 py-2">
        <p className="text-sm font-medium text-red-700">{props.message}</p>
        {hint !== null && <p className="text-sm text-red-600">{hint}</p>}
      </div>
    );
  }

  // Error C — stored but unreadable (201 with `status: "extraction_failed"`). Deliberately amber,
  // not red: the upload itself succeeded, so this is not "the request failed", it is "the file
  // needs a different format" — a different fact that must not look like the same incident as B.
  return (
    <div
      role="alert"
      className="space-y-1 rounded-md border border-amber-200 bg-amber-50 px-3 py-2"
    >
      <p className="text-sm font-medium text-amber-800">{props.message}</p>
    </div>
  );
}
