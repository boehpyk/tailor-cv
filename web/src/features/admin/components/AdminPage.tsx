export interface AdminPageProps {
  /** The signed-in account, from `AccountScope`; roots the access probe's key (AC-34). */
  readonly userId: string;
}

/**
 * The lazy target (default export, for `React.lazy`): the container that reads `useAdminAccess`
 * and renders the `AdminView` (AC-31).
 *
 * SKELETON (T19): a fixed stub — no probe, no view derivation. T21 builds the four views
 * (checking / allowed → `AdminShell` / not_found → `NotFoundPage` / unavailable + Retry).
 */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- the skeleton ignores `userId`; T21 reads it and removes this line
export default function AdminPage(_props: AdminPageProps): React.JSX.Element {
  return <p>Admin (skeleton)</p>;
}
