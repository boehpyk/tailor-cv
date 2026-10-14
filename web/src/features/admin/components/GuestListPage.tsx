export interface GuestListPageProps {
  /** The signed-in admin, from `AccountScope`; roots every query key (AC-40). */
  readonly actorId: string;
}

/**
 * `/admin/guests` (AC-32): `useGuestSessions`; loading / error + Retry / empty / rows with
 * *Load more*; the erase notice from `location.state`.
 *
 * SKELETON (T20): an empty, data-attributed placeholder; T22 builds the views.
 */
export function GuestListPage({ actorId }: GuestListPageProps): React.JSX.Element {
  return <div data-admin-stub="guest-list" data-actor={actorId} />;
}
