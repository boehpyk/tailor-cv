/**
 * The Saved CVs section on `/account` (AC-34, AC-35, AC-44) — a container.
 *
 * Owns the list query (`useSavedBaseCvs`) and the account upload (`useUploadSavedBaseCv`), and
 * renders four deliberately distinct states: loading (`role="status"`), error (`role="alert"` +
 * Retry — never the empty state), empty ("No saved CVs yet" + the upload control) and success (one
 * `SavedBaseCvRow` per CV + the upload control). The upload control states AC-44's retention notice
 * before a file is chosen, and stays enabled at the cap — the server's 409 is rendered (AC-35).
 *
 * Rendered inside `RequireAuth`, so it does not gate on auth itself (S-57 is 2.1's Retry).
 *
 * SKELETON (T25): renders nothing. GREEN is T27; placed on `AccountPage` at T28.
 */
export function SavedBaseCvsSection(): React.JSX.Element | null {
  return null;
}
