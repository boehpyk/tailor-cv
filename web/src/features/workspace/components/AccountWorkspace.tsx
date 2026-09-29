export interface AccountWorkspaceProps {
  /** The signed-in user — handed down by `AccountScope`, and the root of every account key. */
  readonly userId: string;
}

/**
 * The **account** workspace — `/` for a signed-in user (AC-38…AC-42, AC-49, AC-52), rendered
 * inside `AccountScope`, so every scoped hook below talks to `/api/me/` with the bearer and keys
 * its cache under `['auth', 'account', userId]`. A **container**:
 *
 * - **Base CV** — `SavedBaseCvPicker` in `mode: 'select'`; the selection is `useState` here,
 *   validated against the list on render (2.2's `effectiveSelection`); **no** working copy, so no
 *   request to `/api/base-cvs/copies` (AC-39).
 * - **Posting** — 1.2's panel in the account scope (`/api/me/job-postings`), its card saying
 *   *Saved with your history* instead of a date (AC-40).
 * - **Launch** — `POST /api/me/tailoring-runs`, guarded by `isMutating`, navigating to
 *   `/history/{id}`; the latest-run card from `useLatestAccountRun` (AC-41).
 * - **Guest work** — `GuestWorkNotice` names what this browser still holds as a guest (AC-42).
 * - **The promise** — AC-49's sentence, before the first launch.
 *
 * SKELETON (T29): a distinguishable stub; T31 builds it.
 */
export function AccountWorkspace({ userId }: AccountWorkspaceProps): React.JSX.Element {
  return <p data-account-workspace={userId}>AccountWorkspace (skeleton)</p>;
}
